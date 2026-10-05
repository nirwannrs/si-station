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
from .state import describe_states, effective_stat, get_item, is_away, stat_max, xp_needed

# name -> label shown in the Models screen. Adding a helper job means adding it here, plus its
# prompt builder and reply parser below.
HELPER_TASKS = (
    ("resolve_actions", "Understand what the player does"),
    ("suggest_choices", "Suggest replies"),
    ("summarize", "Summarize old turns"),
    ("direct_scene", "Direct the scene (who speaks, expressions, where characters are)"),
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
    ("map", '{"type": "move", "who": WHO, "location": LOCATION_ID}  a character goes somewhere'),
    ("map", '{"type": "lock_travel", "reason": "..."}  {{user}} cannot leave this place for now; the game closes the map to them. The reason is one short sentence the player will see'),
    ("map", '{"type": "unlock_travel"}  {{user}} is free to travel again'),
    ("quests", '{"type": "quest_start", "quest": QUEST_ID}'),
    ("quests", '{"type": "quest_advance", "quest": QUEST_ID}  the current objective was just met'),
    ("quests", '{"type": "quest_fail", "quest": QUEST_ID}'),
)

TRACKED = (("battle", "fights"), ("states", "the state each character is in"), ("inventory", "inventory"), ("equipment", "equipment"), ("money", "money"), ("stats", "stats"),
           ("levels", "levels and experience"), ("skills", "skills"), ("relationships", "relationships"),
           ("map", "locations"), ("quests", "quests"))


def player_action_types(card):
    return set(kind for kind, need, line in PLAYER_ACTIONS if uses(card, need))


LEAVING = """

[When {{user}} leaves a place]
A result saying {{user}} left one place for another means they walked out of the scene that was going on. Do not carry that scene on as if they were still in it. Decide what leaving means and narrate that:
- If someone there would have stopped them (a guard, a captor, an examiner who has not dismissed them), narrate them being stopped and put them back with a move action for "player" to the place they left. If they would be stopped every time, also use lock_travel so they cannot simply walk off again.
- Otherwise they are gone, and the place they left goes on without them. Narrate where they are now. Let what they walked out on have its consequences: people they left react when next met, and a quest that needed them there fails (quest_fail) if its "Fails if" says so or if leaving plainly makes it impossible.
You can also use lock_travel ahead of time, the moment a scene begins that {{user}} could not walk out of. While travel is locked the game state says so. It stays locked until you use unlock_travel, so do that as soon as the story lets them go: when the scene ends, they are dismissed, they escape or talk their way out. Never leave it locked once nothing is holding them."""


def states_reference(card):
    """What each state means and stops, for the narrator."""
    if not uses(card, "states"):
        return ""
    lines = []
    for state in sorted(card.states.values(), key=lambda s: s["id"]):
        blocks = state.get("blocks", [])
        stops = "stops them doing anything" if "all" in blocks else "stops: " + ", ".join(blocks) if blocks else "stops nothing"
        lines.append("- %s. %s (%s%s)" % (_named(state), state.get("description", ""), stops, "; they are out of the scene" if state.get("away") else ""))
    return """

[States]
Each character's current states are in the game state. Keep them true: set a state when the story puts someone in it and clear it when the story ends it. The engine refuses what a state stops {{user}} from doing, so narrate {{user}} as held to it until you clear it. A character who is out of the scene cannot speak or act in it.
""" + "\n".join(lines)


def action_protocol(card):
    """How the narrator reports state changes. Empty for a card that tracks nothing."""
    lines = [line for need, line in NARRATOR_ACTIONS if uses(card, need)]
    if not lines:
        return ""
    tracked = ", ".join(label for need, label in TRACKED if uses(card, need))
    return """\
[Game mechanics]
A game engine tracks %s. It is the source of truth; the state shown to you is exact.

The player's message may come with engine results for things they tried to do. Treat them as fact:
- "done" happened. Narrate it.
- "REJECTED" did not happen. Narrate the attempt failing for the stated reason, in the story's voice (reaching for a pouch that is empty, a door that will not open). Never narrate a rejected action as succeeding.
Never describe {{user}} gaining, losing or using something the engine tracks unless a result or the state says so.

When events you narrate change the tracked state, end your reply with one block:
<actions>{"actions": [ ... ]}</actions>
Use only these, with ids from the state. WHO is "player" or a character id.
%s
Do not repeat anything already listed in the engine results. Leave the block out when nothing changes. The player never sees it.%s%s""" % (tracked, "\n".join(lines), LEAVING if uses(card, "map") else "", states_reference(card))


OPENING_CUE = "(The story begins.)"


def fill(card, state, text):
    return text.replace("{{user}}", state["actors"][PLAYER]["name"]).replace("{{currency}}", card.currency)


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


def _exits(card, location_id):
    here = card.locations.get(location_id)
    if not here:
        return []
    return [l for l in card.data.get("locations", [])
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


def describe_state(card, state):
    me = state["actors"][PLAYER]
    here = card.locations.get(me["location"])
    lines = ["[Current game state]", "Player: %s (player)" % me["name"]]
    if here:
        lines.append("Location: %s. Exits: %s" % (_named(here), ", ".join(_named(l) for l in _exits(card, here["id"])) or "none"))
    if state.get("travel_lock") is not None:
        lines.append("Travel: LOCKED for the player (%s) Use unlock_travel once nothing holds them." % state["travel_lock"])
    lines += _sheet(card, state, PLAYER)

    present, elsewhere = [], []
    for who, actor in sorted(state["actors"].items()):
        if who == PLAYER:
            continue
        doing = (" [%s]" % actor["note"] if actor.get("note") else "") + ("" if actor.get("known", True) else " [the player has not met them yet]")
        if actor["location"] == me["location"] and not is_away(card, actor):
            present.append("- %s (%s)%s%s" % (actor["name"], who, doing, ": " + "; ".join(_sheet(card, state, who)) if _sheet(card, state, who) else ""))
        elif is_away(card, actor):
            elsewhere.append("%s (%s) is away: %s" % (actor["name"], who, describe_states(actor)))
        else:
            place = card.locations.get(actor["location"])
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
    if known and uses(card, "inventory"):
        lines.append("All items that exist: %s" % ", ".join(known))
    return "\n".join(lines)


def describe_quests(card, state):
    active, available = [], []
    for quest in card.data.get("quests", []):
        progress = state["quests"].get(quest["id"])
        if progress is None:
            available.append("- %s: %s" % (_named(quest, "title"), quest.get("description", "")))
        elif progress["status"] == "active":
            stage = quest["stages"][progress["stage"]]
            active.append("- %s. Current objective: %s%s%s" % (
                _named(quest, "title"), stage["description"], " (Met when: %s)" % stage["hint"] if stage.get("hint") else "",
                " (Fails if: %s)" % quest["fail_when"] if quest.get("fail_when") else ""))
    lines = []
    if active:
        lines += ["[Active quests]"] + active
    if available:
        lines += ["[Quests not started yet]"] + available
    return "\n".join(lines)


def describe_cast(card):
    """Everyone in the card, the same on every turn. Who is in the scene right now is in the game state instead."""
    lines = []
    for c in card.data.get("characters", []):
        parts = ["%s: %s" % (c["name"], c["description"])]
        for label, key in (("Personality", "personality"), ("Appearance", "appearance"), ("How they talk", "dialogue_examples")):
            if c.get(key):
                parts.append("%s: %s" % (label, c[key]))
        lines.append("\n".join(parts))
    return "[Characters]\n" + "\n\n".join(lines) if lines else ""


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
    return "\n".join("- %s: %s" % ("done" if r["ok"] else "REJECTED", r["message"]) for r in results)


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

VOLATILE_SLOTS = ("lorebook", "state", "quests")


def narrator_prompt(card, state, preset, player_text, results):
    """Returns (system, messages). One message carries "cache": True, marking the end of the part
    that will be identical next turn; llm.chat_request turns that into the provider's own marker.

    Text blocks above the history slot and every unchanging slot form the system text, in preset
    order. Text blocks below history and the changing slots are attached to the newest player
    message. A block's role is not used yet: all blocks are sent as instructions.
    """
    world = card.data["world"]
    me = state["actors"][PLAYER]
    slots = {
        "world": lambda: "[World]\n%s%s" % (world["description"], "\n\n[Situation at the start]\n" + world["scenario"] if world.get("scenario") else ""),
        "narrator_instructions": lambda: world.get("narrator_instructions", ""),
        "persona": lambda: "[The player's character]\nName: %s%s%s" % (
            me["name"], "\n" + me["description"] if me.get("description") else "",
            "\nAppearance: " + me["appearance"] if me.get("appearance") else ""),
        "characters": lambda: describe_cast(card),
        "lorebook": lambda: describe_lore(card, _recent_text(card, state, player_text)),
        "state": lambda: describe_state(card, state),
        "quests": lambda: describe_quests(card, state),
        "summary": lambda: "[Story so far]\n" + state["summary"] if state["summary"] else "",
        "action_protocol": lambda: action_protocol(card),
    }

    stable, tail, history_on, below_history, has_protocol = [], [], False, False, False
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
            (tail if slot in VOLATILE_SLOTS or (not slot and below_history) else stable).append(text)
    if not has_protocol and action_protocol(card):
        stable.append(action_protocol(card))

    messages = [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": opening(card, state)}]
    for turn in state["history"][state.get("summarized", 0):] if history_on else []:
        messages.append({"role": "user", "content": _turn_message(turn["player"], turn["results"])})
        messages.append({"role": "assistant", "content": turn["narration"]})
    messages[-1]["cache"] = True
    messages.append({"role": "user", "content": "\n\n".join(tail + [_turn_message(player_text, results)])})

    return fill(card, state, "\n\n".join(stable)), [dict(m, content=fill(card, state, m["content"])) for m in messages]


_ACTIONS_BLOCK = re.compile(r"<actions>(.*?)</actions>", re.DOTALL | re.IGNORECASE)


def parse_narration(text):
    """Splits a narrator reply into (story text, actions). A block cut off mid-way is dropped."""
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

def resolver_prompt(card, state, player_text):
    system = """\
You are the rules clerk of a text adventure. Read the player's message and list the game actions the player's character is attempting right now.

Only these actions exist for the player:
""" + "\n".join(line for kind, need, line in PLAYER_ACTIONS if uses(card, need)) + """

Rules:
- Use ids from the game state. If the player names an item that is not in the state at all, use the player's own words as the item value; the engine will reject it.
- List an attempt even when it looks impossible (the item is not in the inventory, there is not enough money). The engine decides. Never drop or correct an attempt.
- Speech, looking around, picking things up from the scene and anything else outside the list above produce no actions; the narrator handles them.
- Do not guess at things the player did not say.

Reply with JSON only: {"actions": [ ... ]}. Use {"actions": []} when nothing applies."""
    user = "%s\n\n[Last narration]\n%s\n\n[Player message]\n%s" % (describe_state(card, state), last_narration(card, state), player_text)
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


def suggest_prompt(card, state, count):
    system = """\
You suggest what the player could do next in a text adventure. Give %d short, distinct options, written in first person as the player ("I ask Mira about the cellar."). Mix talking, acting and exploring. Only suggest using or buying things the game state shows are available. One sentence each.

Reply with JSON only: {"choices": ["...", "..."]}""" % count
    user = "%s\n\n%s\n\n[Last narration]\n%s" % (describe_state(card, state), describe_quests(card, state), last_narration(card, state))
    return system, [{"role": "user", "content": user}]


def parse_suggestions(text, count):
    parsed = extract_json(text) or {}
    choices = parsed.get("choices") if isinstance(parsed.get("choices"), list) else []
    return [c.strip() for c in choices if isinstance(c, str) and c.strip()][:count]


def summary_prompt(card, state, turns):
    system = """\
You keep the running summary of a text adventure so the narrator can remember earlier events. Merge the existing summary with the new scenes into one summary in past tense. Keep names, promises, secrets learned, relationships and unresolved threads. Leave out inventory, money and stats; the game tracks those. At most 250 words. Reply with the summary only."""
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
    place = card.locations.get(actor["location"])
    if is_away(card, actor):
        where = "away"
    elif actor["location"] is not None and actor["location"] == me["location"]:
        where = "with the player"
    else:
        where = "at %s" % _named(place) if place else "not in the story yet, or whereabouts unknown"
    return where + (", %s" % actor["note"] if actor.get("note") else "")


def director_prompt(card, state, paragraphs):
    system = """\
You are the stage director of a visual novel. The narrator's latest text is given as numbered paragraphs. Work out two things from it.

1. Who speaks. For each paragraph:
- speaker: the id of the character whose spoken words make up the paragraph, or null when it is narration, description, or the player's own speech. A paragraph that is mostly one character talking, with a short "she says" around it, belongs to that character.
- expression: how that speaker looks while saying it, chosen only from that character's listed expressions. Use null when speaker is null.

2. Where the characters are now. For each character whose place or activity the text changes or shows, give:
- location: "here" if they end the text in the player's company; a location id if they went to or are at that place; null if they left for somewhere that is not on the list or are otherwise out of reach.
- note: a few words on what they are doing or where they went, such as "tending the bar" or "rode off toward the capital".
Report only what the text states or plainly implies. Leave out characters the text does not mention. A character who arrives or is first met is "here".

Reply with JSON only:
{"paragraphs": [{"n": 1, "speaker": "some_id", "expression": "neutral"}, {"n": 2, "speaker": null, "expression": null}],
 "whereabouts": [{"id": "some_id", "location": "here", "note": "..."}]}"""
    me = state["actors"][PLAYER]
    cast = "\n".join("- %s (id: %s). Expressions: %s. Before this text: %s" % (
        c["name"], c["id"], ", ".join(expressions_of(card, c["id"])), _whereabouts(card, state, c["id"])) for c in card.data.get("characters", []))
    here = card.locations.get(me["location"])
    places = "\n[Locations]\nThe player is at %s.\n%s\n" % (_named(here), ", ".join(_named(l) for l in card.data.get("locations", []))) if here else ""
    numbered = "\n\n".join("%d. %s" % (n + 1, p) for n, p in enumerate(paragraphs))
    return system, [{"role": "user", "content": "[Characters]\n%s\n%s\n[Paragraphs]\n%s" % (cast, places, numbered)}]


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


def parse_whereabouts(text, card):
    """What the director said about where characters are, ready for state.track. Unknown characters are
    dropped; a location that is neither "here", a real place nor null is ignored and only the note kept."""
    parsed = extract_json(text) or {}
    updates = []
    for entry in parsed.get("whereabouts") if isinstance(parsed.get("whereabouts"), list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in card.characters:
            continue
        update = {"id": entry["id"]}
        if "location" in entry and (entry["location"] is None or entry["location"] == "here" or entry["location"] in card.locations):
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
