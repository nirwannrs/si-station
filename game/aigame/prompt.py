"""Builds the prompts for each LLM job and reads their replies.

Every builder returns (system, messages) ready for llm.chat_request. There is one narrator prompt,
assembled from the player's preset, and a set of small helper jobs with fixed prompts. Helper jobs
are listed in HELPER_TASKS so the game can route each one to the main or the utility model.
"""

import re

from .actions import shop_price
from .card import PLAYER
from .llm import extract_json
from .text import keep_marks_paired
from .wording import prompt_text
from .state import available_quests, cast_in_play, named_in, describe_states, effective_stat, game_checked, get_item, is_away, knows_place, place_list, places, stat_max, xp_needed

# name -> label shown in the Models screen. Adding a helper job means adding it here, plus its
# prompt builder and reply parser below.
HELPER_TASKS = (
    ("resolve_actions", "Understand what the player does"),
    ("suggest_choices", "Suggest replies"),
    ("summarize", "Summarize old turns"),
    ("direct_scene", "Direct the scene (who speaks, expressions, where characters are)"),
    ("record_changes", "Keep the books (record what each reply changed)"),
    ("judge_quests", "Judge quest progress"),
)

# Every action is listed with the card system it needs, so a card only teaches the model the
# actions it actually uses. The needs are a rules.features name, or "shops", "map", "quests", "stats"
# for systems that exist when the card defines any.

def uses(card, need):
    if need == "shops":
        return bool(card.shops) and card.has("money") and card.has("inventory")
    if need in ("map", "quests", "stats"):
        return bool({"map": card.locations, "quests": card.quests, "stats": card.stats}[need])
    if need == "battle":
        return card.battle_system
    if need == "new_places":
        return bool(card.locations) and card.data["rules"].get("allow_generated_locations", True)
    if need == "new_items":
        return card.has("inventory") and card.allow_generated_items
    return card.has(need)


# What a player may do just by saying so. Anything else (finding items, gaining money, experience,
# quest progress) has to come from the narrator, so "I find 1000 gold" cannot grant it.
PLAYER_ACTIONS = (
    ("use_item", "inventory", '{"type": "use_item", "item": ID}  eat, drink, read or otherwise use an item'),
    ("use_item", "inventory", '{"type": "use_item", "item": ID, "target": CHARACTER_ID}  use an item on someone else'),
    ("equip", "equipment", '{"type": "equip", "item": ID}  wear or wield an item'),
    ("unequip", "equipment", '{"type": "unequip", "slot": SLOT}  take off what is in a slot (head, body, hands, feet, weapon, offhand, accessory)'),
    ("transfer_item", "inventory", '{"type": "transfer_item", "item": ID, "qty": N, "to": CHARACTER_ID}  hand an item to a character'),
    ("buy", "shops", '{"type": "buy", "shop": SHOP_ID, "item": ID, "qty": N}'),
    ("sell", "shops", '{"type": "sell", "shop": SHOP_ID, "item": ID, "qty": N}'),
    ("move", "map", '{"type": "move", "location": LOCATION_ID}  go to another location'),
    ("use_skill", "skills", '{"type": "use_skill", "skill": SKILL_ID, "target": CHARACTER_ID}  use one of the player\'s skills; leave target out for a skill used on oneself'),
    ("start_battle", "battle", '{"type": "start_battle", "enemies": [CHARACTER_ID, ...]}  attack someone or otherwise start a fight with them'),
)

NARRATOR_ACTIONS = (
    ("inventory", '{"type": "add_item", "who": WHO, "item": ID, "qty": N}  an existing item is found, looted or received from the world'),
    ("new_items", '{"type": "create_item", "who": WHO, "name": "...", "description": "..."}  a new item that is not in the item list'),
    ("inventory", '{"type": "remove_item", "who": WHO, "item": ID, "qty": N}  an item is lost, broken or taken away by the world'),
    ("inventory", '{"type": "transfer_item", "item": ID, "qty": N, "from": WHO, "to": WHO}  one character hands an item to another'),
    ("inventory", '{"type": "use_item", "who": WHO, "item": ID, "target": WHO}  a character uses up an item'),
    ("money", '{"type": "change_money", "who": WHO, "amount": N}  money gained (positive) or lost (negative) outside a shop'),
    ("stats", '{"type": "change_stat", "who": WHO, "stat": STAT_ID, "amount": N}  for example damage is a negative amount'),
    ("skills", '{"type": "use_skill", "who": CHARACTER_ID, "skill": SKILL_ID, "target": WHO}  a character other than the player uses one of their skills'),
    ("skills", '{"type": "unlock_skill", "who": WHO, "skill": SKILL_ID}  someone learns a skill through the story'),
    ("levels", '{"type": "gain_xp", "who": WHO, "amount": N}  experience for something achieved: about 10 for a small success, 30 for a real fight or a clever solution, 100 for a major victory'),
    ("states", '{"type": "set_state", "who": WHO, "state": STATE_ID, "note": "..."}  someone enters a state: falls asleep, is tied up, leaves for a while. The note says how or by whom, in a few words. A state that is not in the list is allowed and simply remembered'),
    ("states", '{"type": "clear_state", "who": WHO, "state": STATE_ID}  the state ends: they wake, break free, come back'),
    ("relationships", '{"type": "change_relationship", "who": CHARACTER_ID, "amount": N}  how that character feels about the player shifts: usually -5 to +5, up to 15 for a moment that truly matters'),
    ("battle", '{"type": "start_battle", "enemies": [CHARACTER_ID, ...]}  a fight breaks out with these characters. The game then runs the fight itself, blow by blow, so end your reply at the moment it starts: do not narrate blows, damage or who wins'),
    ("map", '{"type": "move", "who": WHO, "location": LOCATION_ID}  someone ends up in another location, by any means: walking, a portal, a carriage, being carried. Any location can be reached this way, not only neighbouring ones. Use one for each person who goes'),
    ("new_places", '{"type": "create_location", "name": "...", "description": "...", "temporary": false}  the story has taken someone to a place that is not in the location list at all. Create it, then move them there using its name. Set temporary to true for a place that ceases to exist once everyone has left it (a pocket dimension, a dream, a sinking ship)'),
    ("map", '{"type": "reveal_location", "location": LOCATION_ID}  {{user}} learns that one of the places they do not know of exists and how to reach it: someone tells them, they find a map, they notice the door. It then appears on their map'),
    ("map", '{"type": "lock_travel", "reason": "..."}  {{user}} cannot leave this place for now; the game closes the map to them. The reason is one short sentence the player will see'),
    ("map", '{"type": "unlock_travel"}  {{user}} is free to travel again'),
    ("quests", '{"type": "quest_start", "quest": QUEST_ID}'),
    ("quests", '{"type": "quest_advance", "quest": QUEST_ID, "stage": OBJECTIVE_ID}  the quest\'s current objective, named by its id, is now completely finished: every part of it has happened and is over. Never for an objective that has only begun or is going well'),
    ("quests", '{"type": "quest_fail", "quest": QUEST_ID}'),
)

TRACKED = (("battle", "fights"), ("states", "the state each character is in"), ("inventory", "inventory"), ("equipment", "equipment"), ("money", "money"), ("stats", "stats"),
           ("levels", "levels and experience"), ("skills", "skills"), ("relationships", "relationships"),
           ("map", "locations"), ("quests", "quests"))


def player_action_types(card):
    return set(kind for kind, need, line in PLAYER_ACTIONS if uses(card, need))


# (The wording for leaving a place is among the built-in prompts in wording.py.)


def states_reference(card, record=True):
    """What each state means and stops. record is whether the reader is the one who sets and clears them."""
    if not uses(card, "states"):
        return ""
    lines = []
    for state in sorted(card.states.values(), key=lambda s: s["id"]):
        blocks = state.get("blocks", [])
        stops = "stops them doing anything" if "all" in blocks else "stops: " + ", ".join(blocks) if blocks else "stops nothing"
        lines.append("- %s. %s (%s%s)" % (_named(state), state.get("description", ""), stops, "; they are out of the scene" if state.get("away") else ""))
    duty = ("Keep them true: set a state when the story puts someone in it and clear it when the story ends it. The engine refuses what a state stops {{user}} from doing, so narrate {{user}} as held to it until you clear it."
            if record else "The engine refuses what a state stops {{user}} from doing, so narrate {{user}} as held to it until the story ends it.")
    return """

[States]
Each character's current states are in the game state. %s A character who is out of the scene cannot speak or act in it.
""" % duty + "\n".join(lines)


def action_protocol(card, record=True, prompts=None):
    """The narrator's game-mechanics instructions. Empty for a card that tracks nothing.

    record says who keeps the books. True: the narrator reports changes itself, in an <actions>
    block. False: a bookkeeper reads the narrator's text afterwards, so the narrator is asked for
    nothing but clear prose. That is far less to get right, which matters for smaller models.
    """
    lines = [line for need, line in NARRATOR_ACTIONS if uses(card, need)]
    if not lines:
        return ""
    tracked = ", ".join(label for need, label in TRACKED if uses(card, need))
    opening = prompt_text(prompts, "narrator_mechanics", tracked=tracked)
    if record:
        return opening + "\n" + prompt_text(prompts, "narrator_records", actions="\n".join(lines)) + (
            "\n\n" + prompt_text(prompts, "leaving_records") if uses(card, "map") else "") + states_reference(card)
    return opening + "\n" + prompt_text(prompts, "narrator_prose") + (
        "\n\n" + prompt_text(prompts, "leaving_prose") if uses(card, "map") else "") + states_reference(card, record=False)





# The bookkeeper: a helper that turns the narrator's prose into recorded changes.

def bookkeeper_prompt(card, state, player_text, results, narration, quests=True, prompts=None):
    """state is the game as it stands after the player's own actions were applied. quests is False
    when a separate quest judge is deciding quest progress, so the bookkeeper leaves quests alone."""
    lines = [line.replace("{{user}}", "the player") for need, line in NARRATOR_ACTIONS if uses(card, need) and (quests or need != "quests")]
    checks = []
    if uses(card, "stats"):
        checks.append("- Costs and harm. If someone casts magic, uses an ability or exerts themselves and no result above already charged for it, lower the stat that fuels it (about 1 to 3 for something small, 4 to 6 for something solid, 8 or more for something great). If someone is hurt or healed, change the stat that measures it by a fitting amount.")
    if uses(card, "states"):
        checks.append("- States. For every state the game state lists on anyone, decide whether it still holds at the end of the text and clear_state the ones that ended (they landed, woke, got free, came back). set_state the ones that began.")
    if uses(card, "map"):
        checks.append("- Places. If the player ends the text somewhere other than the Location in the game state, move them there, however they got there and however far it is (a portal, a journey, being taken), including back to where they were if they were stopped from leaving. Move every character who went with them too. If they end up in a place that is not in the game state's lists at all, create_location it first when that action is listed above, then move them there by its name. If the text makes plain they are now held in place, lock_travel; if it lets them go, unlock_travel.")
    if uses(card, "inventory") or uses(card, "money"):
        checks.append("- Belongings. Anything handed over, picked up, found, lost, broken, used up, paid or received. Only a change of hands counts: what someone is merely described as wearing, holding or working with is scenery, not a new item.")
    if uses(card, "quests") and quests:
        checks.append("- Quests. Advance a quest only when its current objective is completely finished, every part of it; one that has begun or is going well is not finished. A quest whose \"Fails if\" has happened is failed; a quest the text gives the player is started.")
    if uses(card, "levels"):
        checks.append("- Experience, when the player has just achieved something.")
    if uses(card, "relationships"):
        checks.append("- Relationships, when a character's feeling toward the player has plainly shifted.")
    if uses(card, "battle"):
        checks.append("- Fights. If a fight breaks out in the text, start_battle with the characters fighting the player, and record nothing of the blows.")
    system = prompt_text(prompts, "record_changes", actions="\n".join(lines), checks="\n".join(checks),
                         states=states_reference(card, record=False).replace("{{user}}", "the player"))
    already = "\n[Engine results already recorded this turn]\n%s\n" % _results_text(results) if results else ""
    earlier = "\n\n".join(t["narration"][-600:] for t in state["history"][-2:])
    user = "%s\n\n%s%s[Player's message]\n%s\n%s\n[Narrator's new text]\n%s" % (
        describe_state(card, state, focus="%s\n%s" % (player_text, narration)), describe_quests(card, state) + "\n\n" if quests and describe_quests(card, state) else "",
        "[Just before, already recorded; for context only]\n%s\n\n" % earlier if earlier else "", player_text, already, narration)
    return system, [{"role": "user", "content": fill(card, state, user)}]


# The quest judge: a helper whose only job is to decide whether quests have moved on.

def judge_prompt(card, state, player_text, narration, turns=5, prompts=None):
    system = prompt_text(prompts, "judge_quests")
    active = []
    waiting = ["- %s: %s" % (_named(quest, "title"), quest.get("description", "")) for quest in available_quests(card, state)]
    for quest in card.data.get("quests", []):
        progress = state["quests"].get(quest["id"])
        if progress is not None and progress["status"] == "active":
            stage = quest["stages"][progress["stage"]]
            entry = "- %s. Current objective [%s], part %d of %d: %s" % (
                _named(quest, "title"), stage["id"], progress["stage"] + 1, len(quest["stages"]), stage["description"])
            if game_checked(card, stage):
                entry += "\n  The game itself marks this objective finished. Answer not_yet for it, unless the quest has failed."
            else:
                entry += "\n  Finished only when: %s" % (stage.get("done_when") or "everything the objective describes has happened and is over")
            if quest.get("fail_when"):
                entry += "\n  Fails if: %s" % quest["fail_when"]
            active.append(entry)
    story = ["Player: %s\nNarrator: %s" % (t["player"], t["narration"][-1500:]) for t in state["history"][-turns:]]
    user = "[Quests in progress]\n%s\n\n[Not started]\n%s\n\n[Recent story, oldest first]\n%s\n\n[Newest]\nPlayer: %s\nNarrator: %s" % (
        "\n".join(active) or "(none)", "\n".join(waiting) or "(none)", "\n\n".join(story) or "(the story has only just begun)", player_text, narration)
    return system, [{"role": "user", "content": fill(card, state, user)}]


def parse_judge(text, card):
    """The judge's verdicts as actions. Each names the objective it judged, which the engine checks
    against the quest's real current objective, so a verdict about the wrong one changes nothing."""
    parsed = extract_json(text) or {}
    actions = []
    for verdict in parsed.get("verdicts") if isinstance(parsed.get("verdicts"), list) else []:
        if not isinstance(verdict, dict) or verdict.get("quest") not in card.quests:
            continue
        if verdict.get("verdict") == "done" and isinstance(verdict.get("objective"), str):
            actions.append({"type": "quest_advance", "quest": verdict["quest"], "stage": verdict["objective"]})
        elif verdict.get("verdict") == "failed":
            actions.append({"type": "quest_fail", "quest": verdict["quest"]})
    for quest in parsed.get("start") if isinstance(parsed.get("start"), list) else []:
        if quest in card.quests:
            actions.append({"type": "quest_start", "quest": quest})
    return actions


def parse_bookkeeper(text):
    """The actions the bookkeeper listed. They are validated like any others when applied."""
    parsed = extract_json(text) or {}
    return [a for a in parsed.get("actions") if isinstance(a, dict)] if isinstance(parsed.get("actions"), list) else []


OPENING_CUE = "(The story begins.)"


def fill(card, state, text):
    """Replaces the placeholders card authors write: {{user}} is the player's name, {{currency}}
    the card's money, and {{char}} the character's name when the card has exactly one (with more
    than one there is no telling who was meant, so it is left alone)."""
    text = text.replace("{{user}}", state["actors"][PLAYER]["name"]).replace("{{currency}}", card.currency)
    if "{{char}}" in text and len(card.characters) == 1:
        text = text.replace("{{char}}", list(card.characters.values())[0]["name"])
    return text


def opening(card, state):
    return fill(card, state, card.data["world"]["opening"])


def last_narration(card, state):
    return state["history"][-1]["narration"] if state["history"] else opening(card, state)


# State descriptions shared by the narrator and the helpers. Ids are shown so actions can cite them.

def _fmt(n):
    return str(int(n)) if n == int(n) else str(n)


def _named(thing, key="name"):
    return "%s (%s)" % (thing[key], thing["id"])


def _stacks(card, state, inventory):
    parts = []
    for item_id, qty in sorted(inventory.items()):
        item = get_item(card, state, item_id)
        parts.append("%s x%d" % (_named(item), qty))
    return ", ".join(parts) or "nothing"


def _equipped(card, state, actor):
    parts = ["%s: %s" % (slot, _named(get_item(card, state, item_id))) for slot, item_id in sorted(actor["equipment"].items())]
    return ", ".join(parts) or "nothing"


def _stats(card, state, who):
    parts = []
    for stat in card.data["rules"]["stats"]:
        value = _fmt(effective_stat(card, state, who, stat["id"]))
        ceiling = stat_max(card, state["actors"][who], stat["id"])
        parts.append("%s %s%s" % (_named(stat), value, "" if ceiling is None else "/" + _fmt(ceiling)))
    return ", ".join(parts)


def _exits(card, state, location_id):
    here = places(card, state).get(location_id)
    if not here:
        return []
    return [l for l in place_list(card, state)
            if l["id"] != here["id"] and (l["id"] in here.get("connections", []) or here["id"] in l.get("connections", []))]


def _skills(card, actor):
    parts = []
    for skill_id, known in sorted(actor["skills"].items()):
        skill = card.skills[skill_id]
        cost = ", ".join("%s %s" % (card.stats[stat]["name"], _fmt(amount)) for stat, amount in sorted(skill.get("cost", {}).items()))
        parts.append("%s%s%s" % (_named(skill), " costs " + cost if cost else "", "" if known["unlocked"] else " [locked]"))
    return ", ".join(parts) or "none"


def _sheet(card, state, who):
    """The tracked facts about one actor, limited to the systems the card uses."""
    actor = state["actors"][who]
    parts = []
    if uses(card, "levels"):
        parts.append("Level %d (experience %s/%s)" % (actor["level"], _fmt(actor["xp"]), _fmt(xp_needed(card, actor))))
    if uses(card, "stats"):
        parts.append("Stats: %s" % _stats(card, state, who))
    if uses(card, "money"):
        parts.append("%s: %s" % (card.currency, _fmt(actor["money"])))
    if uses(card, "inventory"):
        parts.append("Inventory: %s" % _stacks(card, state, actor["inventory"]))
    if uses(card, "equipment"):
        parts.append("Equipped: %s" % _equipped(card, state, actor))
    if uses(card, "skills"):
        parts.append("Skills: %s" % _skills(card, actor))
    if uses(card, "states") and actor.get("states"):
        parts.append("State: %s" % describe_states(actor))
    if uses(card, "relationships") and who != PLAYER:
        parts.append("%s toward the player: %s/100" % (card.relationship_name, _fmt(actor["relationship"])))
    return parts


def _unknown_places(card, state, text=None):
    """Places the player has not found. Given the text in play, only the ones that could come up
    now: next door to where the player is, or named by that text or by the objective being played.
    A hidden place on the far side of the map is nothing the reader needs yet."""
    hidden = [l for l in place_list(card, state) if not knows_place(state, l["id"])]
    if text is None or not hidden:
        return hidden
    here = state["actors"][PLAYER]["location"]
    near = set(l["id"] for l in _exits(card, state, here)) if here in places(card, state) else set()
    for quest_id, progress in state["quests"].items():
        if progress["status"] == "active" and quest_id in card.quests:
            stage = card.quests[quest_id]["stages"][progress["stage"]]
            text += "\n" + "\n".join(stage.get(key) or "" for key in ("description", "guidance", "hint", "done_when"))
    return [l for l in hidden if l["id"] in near or (l.get("name") and re.search(r"(?<!\w)%s(?!\w)" % re.escape(l["name"]), text, re.I))]


def describe_scene(card, state):
    """A short briefing for the narrator on the scene as it stands: where it is, who is in it and
    what was going on. The facts are the engine's; the one line on what is happening is written by
    the scene director after each reply. It names only who is there. People who are somewhere else
    are not listed here, because a model tends to use whoever it is shown."""
    me = state["actors"][PLAYER]
    here = places(card, state).get(me["location"])
    present = []
    for who, actor in sorted(state["actors"].items()):
        if who != PLAYER and actor["location"] is not None and actor["location"] == me["location"] and not is_away(card, actor):
            present.append(actor["name"] + (" (%s)" % actor["note"] if actor.get("note") else ""))
    lines = ["[The scene right now]"]
    if here:
        lines.append("Place: %s." % here["name"])
    if card.characters:
        lines.append("With %s: %s" % (me["name"], "; ".join(present) + "." if present else "nobody else."))
    if state.get("scene"):
        lines.append("What is going on: %s" % state["scene"])
    if card.characters:
        lines.append("These are the people in the scene. Someone else joins it only when your reply shows them arriving.")
    return "\n".join(lines)


def describe_state(card, state, secrets=True, focus=None, cast=None, items=True):
    """secrets is False for helpers that speak for the player (their suggested replies, reading
    their intent), which must not be told about places the player has not discovered.

    The rest keeps the state down to what the reader needs this turn. focus is the text in play
    (what the player typed, what the story just said): a character in the scene gets their full
    sheet when that text names them, and one line otherwise. cast is who has entered the story;
    only they are mentioned as being elsewhere. items is whether to list every item that exists,
    which only a reader who records changes by item id needs. Left out, everything is shown."""
    me = state["actors"][PLAYER]
    here = places(card, state).get(me["location"])
    lines = ["[Current game state]", "Player: %s (player)" % me["name"]]
    if here:
        exits = [_named(l) + ("" if knows_place(state, l["id"]) else " [the player does not know of it yet]")
                 for l in _exits(card, state, here["id"]) if secrets or knows_place(state, l["id"])]
        lines.append("Location: %s%s. Exits on foot: %s" % (_named(here), " [made by the story; temporary]" if here.get("temporary") else "", ", ".join(exits) or "none"))
        ## Named with their ids so that whoever records a journey, a portal or a forced move can say where it went.
        near = set(l["id"] for l in _exits(card, state, here["id"]))
        further = [_named(l) for l in place_list(card, state) if l["id"] != here["id"] and l["id"] not in near and knows_place(state, l["id"])]
        if further:
            lines.append("Other places on the map, not reachable on foot from here: %s" % ", ".join(further))
    if secrets and _unknown_places(card, state, focus):
        lines.append("Places the player does not know of yet (not on their map; reveal one with reveal_location when the story shows it to them): %s" % "; ".join(
            "%s%s" % (_named(l), ": " + l["description"] if l.get("description") else "") for l in _unknown_places(card, state, focus)))
    if state.get("travel_lock") is not None:
        lines.append("Travel: LOCKED for the player (%s) Use unlock_travel once nothing holds them." % state["travel_lock"])
    lines += _sheet(card, state, PLAYER)

    present, elsewhere = [], []
    in_focus = set(named_in(card, focus)) if focus is not None else set()
    for who, actor in sorted(state["actors"].items()):
        if who == PLAYER:
            continue
        doing = (" [%s]" % actor["note"] if actor.get("note") else "") + ("" if actor.get("known", True) else " [not met yet]")
        if actor["location"] == me["location"] and not is_away(card, actor):
            sheet = _sheet(card, state, who) if focus is None or who in in_focus else _brief(card, state, who)
            present.append("- %s (%s)%s%s" % (actor["name"], who, doing, ": " + "; ".join(sheet) if sheet else ""))
        elif cast is not None and (who not in cast or (actor["location"] is None and not actor.get("note") and not is_away(card, actor))):
            continue        # not in the story, or in it with no known whereabouts: there is nothing to say about where they are
        elif is_away(card, actor):
            elsewhere.append("%s (%s) is away: %s" % (actor["name"], who, describe_states(actor)))
        else:
            place = places(card, state).get(actor["location"])
            elsewhere.append("%s (%s) at %s%s" % (actor["name"], who, _named(place) if place else "an unknown place", doing))
    if present:
        lines += ["Characters here:"] + present
    if elsewhere:
        lines.append("Elsewhere: %s" % "; ".join(elsewhere))

    for shop in card.data.get("shops", []) if uses(card, "shops") else []:
        if shop.get("location") and shop["location"] != me["location"]:
            continue
        stock = []
        for item_id, left in sorted(state["shops"][shop["id"]].items()):
            stock.append("%s %s %s%s" % (_named(get_item(card, state, item_id)), _fmt(shop_price(card, state, shop["id"], item_id)),
                                         card.currency, "" if left is None else " (%d left)" % left))
        lines.append("Shop here: %s sells %s" % (_named(shop), "; ".join(stock) or "nothing"))

    known = [_named(i) for i in card.data.get("items", [])] + [_named(i) for i in state["generated_items"].values()]
    if known and uses(card, "inventory") and items:
        lines.append("All items that exist: %s" % ", ".join(known))
    return "\n".join(lines)


def stage_guidance(stage):
    """How the narrator should play an objective. "hint" is the older name for the same thing."""
    return stage.get("guidance") or stage.get("hint") or ""


def _brief(card, state, who):
    """One character in a line: what the scene needs of someone who is there but not being dealt
    with right now. Their state, the stat fights are fought over, and how they feel about the player."""
    actor = state["actors"][who]
    parts = []
    health = card.battle.get("health_stat") if uses(card, "stats") else None
    if health and health in card.stats:
        stat = card.stats[health]
        top = stat_max(card, actor, health)
        parts.append("%s %s%s" % (stat["name"], _fmt(effective_stat(card, state, who, health)), "/%s" % _fmt(top) if top is not None else ""))
    if uses(card, "states") and actor.get("states"):
        parts.append("State: %s" % describe_states(actor))
    if uses(card, "relationships"):
        parts.append("%s toward the player: %s/100" % (card.relationship_name, _fmt(actor["relationship"])))
    return parts


def describe_quests(card, state, detail="narrator"):
    """detail "narrator" includes how to play each objective and when it is over. "player" gives
    only what the player can see in their quest log, for helpers that speak for the player."""
    active = []
    available = ["- %s: %s" % (_named(quest, "title"), quest.get("description", "")) for quest in available_quests(card, state)]
    for quest in card.data.get("quests", []):
        progress = state["quests"].get(quest["id"])
        if progress is not None and progress["status"] == "active":
            stage = quest["stages"][progress["stage"]]
            line = "- %s. Current objective [%s], part %d of %d: %s" % (
                _named(quest, "title"), stage["id"], progress["stage"] + 1, len(quest["stages"]), stage["description"])
            if detail == "narrator":
                if stage_guidance(stage):
                    line += "\n  How to play it: %s" % stage_guidance(stage)
                if stage.get("done_when"):
                    line += "\n  It is finished only when: %s" % stage["done_when"]
                if quest.get("fail_when"):
                    line += "\n  Fails if: %s" % quest["fail_when"]
            active.append(line)
    lines = []
    if active:
        lines += ["[Active quests]"] + active
    if available and detail == "narrator":
        lines += ["[Quests that can begin now]"] + available
    return "\n".join(lines)


def arrivals(card, state, player_text):
    """Ids of the characters entering the story with this turn: named by what the player just
    typed or by the last reply, or now with the player, and not described to the story model yet.
    The game keeps them with the turn (turn["cast"]) so the turn's message never changes."""
    known = state.get("cast", [])
    return [c for c in cast_in_play(card, state, "%s\n%s" % (player_text, last_narration(card, state))) if c not in known]


def _entering(card, ids):
    text = describe_cast(card, ids, heading="[Entering the story]") if ids else ""
    return text + "\n\n" if text else ""


def describe_cast(card, cast=None, heading="[Characters]"):
    """Who the characters are. cast is the ids of those who have entered the story, in the order
    they did; only they are described, in that order, so the text only ever grows at its end.
    Left out, everyone in the card is described."""
    lines = []
    chosen = card.data.get("characters", []) if cast is None else [card.characters[c] for c in cast if c in card.characters]
    for c in chosen:
        parts = ["%s: %s" % (c["name"], c["description"])]
        ## ai_notes is for the model alone; the game never shows it to the player.
        for label, key in (("Personality", "personality"), ("Appearance", "appearance"), ("How they talk", "dialogue_examples"), ("Notes for the narrator", "ai_notes")):
            if c.get(key):
                parts.append("%s: %s" % (label, c[key]))
        lines.append("\n".join(parts))
    return heading + "\n" + "\n\n".join(lines) if lines else ""


def describe_lore(card, recent_text):
    low = recent_text.lower()
    hits = [e for e in card.data.get("lorebook", [])
            if e.get("always_on") or any(k.lower() in low for k in e.get("keys", []))]
    hits.sort(key=lambda e: -e.get("priority", 0))
    return "[Background knowledge]\n" + "\n".join("- " + e["content"] for e in hits) if hits else ""


def _recent_text(card, state, player_text, turns=3):
    parts = [player_text]
    for turn in state["history"][-turns:]:
        parts += [turn["player"], turn["narration"]]
    if len(state["history"]) < turns:
        parts.append(card.data["world"]["opening"])
    return "\n".join(parts)


def _results_text(results):
    kinds = {True: "done", False: "REJECTED", None: "UP TO YOU"}
    return "\n".join("- %s: %s" % (kinds[r["ok"]], r["message"]) for r in results)


def _turn_message(player_text, results):
    if not results:
        return player_text
    return "%s\n\n[Engine results]\n%s" % (player_text, _results_text(results))


# The narrator.
#
# Providers bill far less for the start of a prompt when it is byte-for-byte what they saw on the
# previous request. So the prompt is built in two parts:
#   - a prefix that only ever grows: the instructions, the card, and the finished turns;
#   - a tail that changes every turn: lore that just became relevant, the game state, quests, and
#     the player's newest message.
# Whatever a preset's block order says, the changing slots always travel in the tail. Putting one
# in the prefix would make every turn pay full price for the whole conversation.

# The summary is here too. It only changes when old turns are folded into it, which already
# forces the history to be sent afresh; keeping it out of the instructions means those, at least,
# stay cached through a summarization.
VOLATILE_SLOTS = ("summary", "lorebook", "state", "quests")


def narrator_prompt(card, state, preset, player_text, results, record=True):
    """Returns (system, messages). One message carries "cache": True, marking the end of the part
    that will be identical next turn; llm.chat_request turns that into the provider's own marker.

    Text blocks above the history slot and every unchanging slot form the system text, in preset
    order. The newest message is built in three parts: the changing slots (state, quests, lore),
    then what the player said with the engine's results, then the text blocks placed below
    history, so that a reminder is the last thing read. A block's role is not used yet: all blocks
    are sent as instructions.
    """
    world = card.data["world"]
    me = state["actors"][PLAYER]
    ## What is in play this turn decides who is described and in how much detail. See state.cast_in_play.
    in_play = "%s\n%s" % (player_text, last_narration(card, state))
    cast = cast_in_play(card, state, in_play)
    ## Someone who enters the story is described in the message of the turn they enter, and that
    ## message is sent unchanged from then on. Describing them among the instructions instead would
    ## change the start of the prompt, and the provider would have to read the whole story afresh.
    ## Once that turn is folded into the summary, they are described with the rest.
    entering = arrivals(card, state, player_text)
    live = state["history"][state.get("summarized", 0):]
    settled = [c for c in cast if c not in entering and not any(c in turn.get("cast", ()) for turn in live)]
    slots = {
        "world": lambda: "[World]\n%s%s" % (world["description"], "\n\n[Situation at the start]\n" + world["scenario"] if world.get("scenario") else ""),
        "narrator_instructions": lambda: world.get("narrator_instructions", ""),
        "persona": lambda: "[The player's character]\nName: %s%s%s" % (
            me["name"], "\n" + me["description"] if me.get("description") else "",
            "\nAppearance: " + me["appearance"] if me.get("appearance") else ""),
        "characters": lambda: describe_cast(card, settled),
        "lorebook": lambda: describe_lore(card, _recent_text(card, state, player_text)),
        "state": lambda: describe_scene(card, state) + "\n\n" + describe_state(card, state, focus=in_play, cast=cast, items=record),
        "quests": lambda: describe_quests(card, state),
        "summary": lambda: "[Story so far]\n" + state["summary"] if state["summary"] else "",
        "action_protocol": lambda: action_protocol(card, record, preset.get("prompts")),
    }

    stable, tail, closing, history_on, below_history, has_protocol = [], [], [], False, False, False
    for block in preset["blocks"]:
        slot = block.get("slot") if block["kind"] == "slot" else None
        if slot == "action_protocol":
            has_protocol = True
        elif not block.get("enabled", True):
            continue
        if slot == "history":
            history_on = below_history = True
            continue
        text = slots[slot]() if slot else block.get("content", "")
        if text:
            ## The changing parts (state, quests, lore) go in front of what the player just said. An
            ## instruction placed below the story goes after it, as the very last thing the model
            ## reads before it writes: that is where a reminder is heeded most.
            (tail if slot in VOLATILE_SLOTS else closing if not slot and below_history else stable).append(text)
    if not has_protocol and action_protocol(card, record, preset.get("prompts")):
        stable.append(action_protocol(card, record, preset.get("prompts")))

    messages = [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": opening(card, state)}]
    for turn in live if history_on else []:
        messages.append({"role": "user", "content": _entering(card, turn.get("cast")) + _turn_message(turn["player"], turn["results"])})
        messages.append({"role": "assistant", "content": turn["narration"]})
    messages[-1]["cache"] = True
    messages.append({"role": "user", "content": "\n\n".join(tail + [_entering(card, entering) + _turn_message(player_text, results)] + closing)})

    return fill(card, state, "\n\n".join(stable)), [dict(m, content=fill(card, state, m["content"])) for m in messages]


_ACTIONS_BLOCK = re.compile(r"<actions>(.*?)</actions>", re.DOTALL | re.IGNORECASE)


# A model that copies the prompt's own layout into its reply: an "[Engine results]" heading and
# result lines it made up. Left in, the player would read it, and the bookkeeper would take the
# made-up line for something already recorded and record nothing.
_ECHOED_RESULTS = re.compile(r"^[ \t]*(\[Engine results[^\]\n]*\]|- (done|REJECTED|UP TO YOU)\b[^\n]*)[ \t]*\n?", re.M)


def parse_narration(text):
    """Splits a narrator reply into (story text, actions). A block cut off mid-way is dropped."""
    text = _ECHOED_RESULTS.sub("", text)
    actions = []
    for block in _ACTIONS_BLOCK.findall(text):
        parsed = extract_json(block)
        if parsed and isinstance(parsed.get("actions"), list):
            actions += parsed["actions"]
    story = _ACTIONS_BLOCK.sub("", text)
    cut = story.lower().find("<actions>")
    if cut >= 0:
        story = story[:cut]
    return story.strip(), actions


# Helper jobs.

def resolver_prompt(card, state, player_text, prompts=None):
    system = prompt_text(prompts, "resolve_actions", actions="\n".join(line for kind, need, line in PLAYER_ACTIONS if uses(card, need)))
    user = "%s\n\n[Last narration]\n%s\n\n[Player message]\n%s" % (
        describe_state(card, state, secrets=False, focus="%s\n%s" % (player_text, last_narration(card, state)), cast=cast_in_play(card, state, player_text)),
        last_narration(card, state), player_text)
    return system, [{"role": "user", "content": user}]


def parse_resolver(text, card):
    """Actions the player may take by saying so, always acting as the player."""
    parsed = extract_json(text) or {}
    allowed = player_action_types(card)
    actions = []
    for action in parsed.get("actions") if isinstance(parsed.get("actions"), list) else []:
        if isinstance(action, dict) and action.get("type") in allowed:
            action = dict(action)
            action.pop("who", None)
            if action["type"] == "transfer_item":
                action["from"] = PLAYER
            actions.append(action)
    return actions


def suggest_prompt(card, state, count, prompts=None):
    system = prompt_text(prompts, "suggest_choices", count=str(count))
    user = "%s\n\n%s\n\n[Last narration]\n%s" % (
        describe_state(card, state, secrets=False, focus=last_narration(card, state), cast=cast_in_play(card, state), items=False),
        describe_quests(card, state, "player"), last_narration(card, state))
    return system, [{"role": "user", "content": user}]


def parse_suggestions(text, count):
    parsed = extract_json(text) or {}
    choices = parsed.get("choices") if isinstance(parsed.get("choices"), list) else []
    return [c.strip() for c in choices if isinstance(c, str) and c.strip()][:count]


def summary_prompt(card, state, turns, prompts=None):
    system = prompt_text(prompts, "summarize")
    scenes = "\n\n".join("Player: %s\nNarrator: %s" % (t["player"], t["narration"]) for t in turns)
    user = "[Existing summary]\n%s\n\n[New scenes]\n%s" % (state["summary"] or "(none yet)", scenes)
    return system, [{"role": "user", "content": fill(card, state, user)}]


# The scene director: decides, for each paragraph the narrator wrote, who is speaking and how they look.

def split_paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def expressions_of(card, char_id):
    return sorted(card.characters[char_id].get("sprites") or {}) or ["neutral"]


def _whereabouts(card, state, who):
    me, actor = state["actors"][PLAYER], state["actors"][who]
    place = places(card, state).get(actor["location"])
    if is_away(card, actor):
        where = "away"
    elif actor["location"] is not None and actor["location"] == me["location"]:
        where = "with the player"
    else:
        where = "at %s" % _named(place) if place else "not in the story yet, or whereabouts unknown"
    return where + (", %s" % actor["note"] if actor.get("note") else "")


def _looks(character):
    """A short line the director can recognise an unnamed character by."""
    text = " ".join((character.get("appearance") or character.get("description") or "").split())
    return "Looks: %s " % (text[:160].rstrip(".") + ".") if text else ""


def director_prompt(card, state, paragraphs, prompts=None):
    system = prompt_text(prompts, "direct_scene")
    me = state["actors"][PLAYER]
    cast = "\n".join("- %s (id: %s). %sExpressions: %s. Before this text: %s" % (
        c["name"], c["id"], _looks(c), ", ".join(expressions_of(card, c["id"])), _whereabouts(card, state, c["id"])) for c in card.data.get("characters", []))
    here = places(card, state).get(me["location"])
    listing = "\n[Locations]\nThe player is at %s.\n%s\n" % (_named(here), ", ".join(_named(l) for l in place_list(card, state))) if here else ""
    if _unknown_places(card, state, "\n".join(paragraphs)):
        listing += "\n[Places the player does not know of yet]\n%s\n" % "\n".join(
            "- %s%s" % (_named(l), ": " + l["description"] if l.get("description") else "") for l in _unknown_places(card, state, "\n".join(paragraphs)))
    numbered = "\n\n".join("%d. %s" % (n + 1, p) for n, p in enumerate(paragraphs))
    return system, [{"role": "user", "content": "[Characters]\n%s\n%s\n[Paragraphs]\n%s" % (cast, listing, numbered)}]


def parse_direction(text, card, count):
    """One {"speaker", "expression"} per paragraph. Anything the director got wrong becomes plain narration."""
    parsed = extract_json(text) or {}
    direction = [{"speaker": None, "expression": None} for _ in range(count)]
    for position, entry in enumerate(parsed.get("paragraphs") if isinstance(parsed.get("paragraphs"), list) else []):
        if not isinstance(entry, dict):
            continue
        n = entry.get("n")
        index = n - 1 if isinstance(n, int) and not isinstance(n, bool) else position
        speaker = entry.get("speaker")
        if 0 <= index < count and isinstance(speaker, str) and speaker in card.characters:
            expression = entry.get("expression")
            known = expressions_of(card, speaker)
            direction[index] = {"speaker": speaker, "expression": expression if expression in known else known[0] if "neutral" not in known else "neutral"}
    return direction


def parse_scene(text):
    """The director's line on what is going on, or "" when it gave none."""
    scene = (extract_json(text) or {}).get("scene")
    return " ".join(scene.split())[:400] if isinstance(scene, str) else ""


def parse_revealed(text, card, state=None):
    """Location ids the director says the text revealed. Anything that is not a real location is dropped."""
    parsed = extract_json(text) or {}
    known = places(card, state) if state else card.locations
    return [l for l in parsed.get("revealed") if isinstance(l, str) and l in known] if isinstance(parsed.get("revealed"), list) else []


def parse_whereabouts(text, card, state=None):
    """What the director said about where characters are, ready for state.track. Unknown characters are
    dropped; a location that is neither "here", a real place nor null is ignored and only the note kept."""
    parsed = extract_json(text) or {}
    known = places(card, state) if state else card.locations
    updates = []
    for entry in parsed.get("whereabouts") if isinstance(parsed.get("whereabouts"), list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in card.characters:
            continue
        update = {"id": entry["id"]}
        if "location" in entry and (entry["location"] is None or entry["location"] == "here" or entry["location"] in known):
            update["location"] = entry["location"]
        if isinstance(entry.get("note"), str):
            update["note"] = entry["note"].strip()[:120]
        if len(update) > 1:
            updates.append(update)
    return updates


def split_for_display(paragraph, limit=320):
    """Breaks a long paragraph at sentence ends so each piece fits the text box."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?\"\u201d])\s+", paragraph):
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current)
            current = sentence
        else:
            current = (current + " " + sentence).strip()
    return keep_marks_paired(pieces + ([current] if current else []))
