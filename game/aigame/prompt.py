"""Builds the prompts for each LLM job and reads their replies.

Every builder returns (system, messages) ready for llm.chat_request. There is one narrator prompt,
assembled from the player's preset, and a set of small helper jobs with fixed prompts. Helper jobs
are listed in HELPER_TASKS so the game can route each one to the main or the utility model.
"""

import re

from .actions import paid_lately, shop_price
from .card import PLAYER
from .llm import extract_json
from . import clock, journal
from .text import keep_marks_paired, muffled
from .wording import prompt_text
from .state import available_quests, cast_in_play, named_in, describe_states, limits, effective_stat, game_checked, get_item, is_away, knows_place, place_list, places, stat_max, xp_needed

# name -> label shown in the Models screen. Adding a helper job means adding it here, plus its
# prompt builder and reply parser below.
HELPER_TASKS = (
    ("resolve_actions", "Understand what the player does"),
    ("suggest_choices", "Suggest replies"),
    ("summarize", "Summarize old turns"),
    ("direct_scene", "Direct the scene (who speaks, expressions, where characters are)"),
    ("record_changes", "Keep the books (record what each reply changed)"),
    ("judge_quests", "Judge quest progress"),
    ("write_journal", "Keep the journal (remember each scene)"),
    ("move_world", "Move the world on (where people go while the player is elsewhere)"),
    ("keep_time", "Keep the clock (put the time and place line right when it slips)"),
    ("recall_memory", "Remember (pick the earlier scenes this turn calls for)"),
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
    ("use_item", "inventory", '{"type": "use_item", "item": ID}  eat, drink, apply or otherwise use up an item, now. Showing it, holding it, looking at it, offering it, mentioning it or saying what one will do with it later is not using it'),
    ("use_item", "inventory", '{"type": "use_item", "item": ID, "target": CHARACTER_ID}  use an item on someone else, now'),
    ("equip", "equipment", '{"type": "equip", "item": ID}  put on an item or take it in hand to keep it there. Only when the player says they do so. For an item whose id starts with gen_, add "slot": SLOT to say where on the body it goes (head, body, hands, feet, weapon, offhand, accessory)'),
    ("unequip", "equipment", '{"type": "unequip", "slot": SLOT}  take off what is in a slot (head, body, hands, feet, weapon, offhand, accessory)'),
    ("transfer_item", "inventory", '{"type": "transfer_item", "item": ID, "qty": N, "to": CHARACTER_ID}  an item leaves the player\'s hands for good: given, handed over, paid with. Showing it to someone, holding it out to be looked at, or offering it and waiting for an answer is not handing it over'),
    ("buy", "shops", '{"type": "buy", "shop": SHOP_ID, "item": ID, "qty": N}  the player buys, orders or pays for it now. Asking the price, asking what there is, haggling or saying they might buy is not buying'),
    ("sell", "shops", '{"type": "sell", "shop": SHOP_ID, "item": ID, "qty": N}  the player sells it now. Asking what it would fetch is not selling'),
    ("move", "map", '{"type": "move", "location": LOCATION_ID}  go to another location, now, in this message. Talking about going, suggesting it, agreeing to it or asking about it is not going'),
    ("use_skill", "skills", '{"type": "use_skill", "skill": SKILL_ID, "target": CHARACTER_ID}  use one of the player\'s skills, now; leave target out for a skill used on oneself. Talking about a skill, describing it or threatening to use it is not using it'),
    ("start_battle", "battle", '{"type": "start_battle", "enemies": [CHARACTER_ID, ...]}  the player strikes at someone or otherwise starts a fight with them, now. A threat, an insult, a warning or a hand on a weapon is not an attack'),
)

NARRATOR_ACTIONS = (
    ("inventory", '{"type": "add_item", "who": WHO, "item": ID, "qty": N}  an existing item is found, looted or received from the world'),
    ("new_items", '{"type": "create_item", "who": WHO, "name": "...", "description": "...", "kind": KIND, "slot": SLOT, "effects": [{"stat": STAT_ID, "amount": N}]}  a new item that is not in the item list. kind, slot and effects are only for an item that needs them: kind is "consumable" for something used up, or "equipment" for something worn or held, which takes a slot (head, body, hands, feet, weapon, offhand, accessory). effects is what it does to a stat when used, or while worn'),
    ("new_items", '{"type": "change_item", "item": ID, "kind": KIND, "slot": SLOT, "effects": [...], "name": "...", "description": "..."}  an item the story itself made (its id starts with gen_) has become something else. Give only what changes; kind "misc" makes it a plain thing again'),
    ("inventory", '{"type": "remove_item", "who": WHO, "item": ID, "qty": N}  an item is lost, broken or taken away by the world, whether it was carried or worn'),
    ("equipment", '{"type": "equip", "who": WHO, "item": ID}  someone ends the text wearing or holding an item they have. Not for what {{user}} put on by their own listed action'),
    ("equipment", '{"type": "unequip", "who": WHO, "item": ID}  something worn or held comes off and stays with its owner. When it leaves them, use remove_item or transfer_item'),
    ("inventory", '{"type": "transfer_item", "item": ID, "qty": N, "from": WHO, "to": WHO}  one character hands an item to another'),
    ("inventory", '{"type": "use_item", "who": WHO, "item": ID, "target": WHO}  a character uses up an item'),
    ("money", '{"type": "change_money", "who": WHO, "amount": N}  money gained (positive) or lost (negative) outside a shop'),
    ("stats", '{"type": "change_stat", "who": WHO, "stat": STAT_ID, "amount": N}  what someone has of a stat right now goes down or up: spent, hurt, healed, recovered. Damage is a negative amount'),
    ("stats", '{"type": "change_stat_max", "who": WHO, "stat": STAT_ID, "amount": N}  the most someone can have of a stat changes for good: real growth the text shows (long training completed, a power awakened), or a lasting loss (a crippling wound, a curse). Rare. Never for ordinary spending, harm or rest'),
    ("skills", '{"type": "use_skill", "who": CHARACTER_ID, "skill": SKILL_ID, "target": WHO}  a character other than the player uses one of their skills'),
    ("skills", '{"type": "unlock_skill", "who": WHO, "skill": SKILL_ID}  someone learns a skill through the story'),
    ("levels", '{"type": "gain_xp", "who": WHO, "amount": N}  experience for something achieved: about 10 for a small success, 30 for a real fight or a clever solution, 100 for a major victory'),
    ("states", '{"type": "set_state", "who": WHO, "state": STATE_ID, "note": "..."}  someone enters a state, as [States] describes one: falls asleep, is taken prisoner, sinks into grief. state is an id from that list, or a short name of your own for another lasting condition. The note says how it came about or who caused it, in a few words. With a name of your own, add "stops": [...] when the condition keeps them from something: any of speech, sight, hearing, move, items, equipment, skills, trade, attack, or all for every action. Leave it out for a condition that stops nothing'),
    ("states", '{"type": "clear_state", "who": WHO, "state": STATE_ID}  the state ends: they wake, break free, come back'),
    ("relationships", '{"type": "change_relationship", "who": CHARACTER_ID, "amount": N}  how that character feels about the player shifts: usually -5 to +5, up to 15 for a moment that truly matters'),
    ("battle", '{"type": "start_battle", "enemies": [CHARACTER_ID, ...]}  a fight breaks out with these characters. The game then runs the fight itself, blow by blow, so end your reply at the moment it starts: do not narrate blows, damage or who wins'),
    ("map", '{"type": "move", "who": WHO, "location": LOCATION_ID}  someone has arrived in another location by the end of the text, by any means: walking, a portal, a carriage, being carried. Any location can be reached this way, not only neighbouring ones. Use one for each person who went. Only for an arrival: a plan to go, an agreement to go, or a journey begun but not finished moves nobody'),
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


# What a state is, said once and in the same words to whoever reads or records them. A model that
# is only told "set a state when someone enters one" records every passing act as a state.
STATE_MEANING = """\
A state is something that is true of a character for a while. It began at some point, it is still going on, and it will stay true over the coming turns until something in the story ends it. It can be of the body (asleep, wounded, poisoned, exhausted), of their situation (tied up, kidnapped, in hiding, away on an errand, in disguise) or of the mind (grieving, furious with {{user}}, smitten, terrified). States are how the story remembers what someone is in the middle of, so that three turns later the sleeper is still asleep, the prisoner is still gone and the one in mourning has not cheerfully forgotten.
What someone does in a moment is not a state: charging at an opponent, shouting, drawing a sword, answering a question, a flash of annoyance. The test is whether it would still be true several turns from now if nothing changed it. If not, it is simply part of the story.
A state that keeps someone from acting begins only once the text shows them actually unable to: a hold, a blow or a spell that is being tried, resisted or fought over is not yet a state.
Someone can be in several states at once, and each is its own: a prisoner who is tied up, gagged and blindfolded is in three states, and taking the gag off ends one of them and leaves the other two. A state that has ended is removed; it is never kept on with a note saying it is over."""


def states_reference(card, record=True):
    """What a state is, and what each of the card's states means and stops. record is whether the
    reader is the one who sets and clears them."""
    if not uses(card, "states"):
        return ""
    lines = []
    for state in sorted(card.states.values(), key=lambda s: s["id"]):
        blocks = state.get("blocks", [])
        rest = [b for b in blocks if b != "all"]
        stops = ("stops every action" + (" and " + ", ".join(rest) if rest else "")) if "all" in blocks else "stops: " + ", ".join(blocks) if blocks else "stops nothing"
        lines.append("- %s. %s (%s%s)" % (_named(state), state.get("description", ""), stops, "; they are out of the scene" if state.get("away") else ""))
    ## A name of the model's own stops nothing, so someone "kidnapped" under one would still be standing in the room.
    leaving = [_named(state) for state in sorted(card.states.values(), key=lambda s: s["id"]) if state.get("away")]
    gone = (" When what happens takes someone out of the scene (kidnapped, carried off, sent away, lost), use %s for it and say what happened in the note; a name of your own would leave them standing in the room." % " or ".join(leaving)) if leaving else ""
    held = ("What a state stops {{user}} from doing does not happen just because they say so. When they try, you are told, and you decide whether it gets them past what holds them, by who they are, what they can do and what is holding them."
            " If it does, the state is over; until then they are held to it.")
    duty = ("Keep them true: set a state when the story puts someone in it and clear it when the story ends it. " if record else "Write everyone as being in the states they are in. ") + held
    return """

[States]
%s
Each character's current states are in the game state. %s A character who is out of the scene cannot speak or act in it.
These states have rules in this game:
%s
Any other lasting condition can be a state as well, under a short name of two or three words ("grieving", "in disguise"). It stops what you say it stops when you set it, nothing otherwise, and it is remembered.%s""" % (
        STATE_MEANING, duty, "\n".join(lines), gone)


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

def bookkeeper_prompt(card, state, player_text, results, narration, quests=True, prompts=None, clock=None):
    """state is the game as it stands after the player's own actions were applied. quests is False
    when a separate quest judge is deciding quest progress, so the bookkeeper leaves quests alone."""
    lines = [line.replace("{{user}}", "the player") for need, line in NARRATOR_ACTIONS if uses(card, need) and (quests or need != "quests")]
    checks = []
    if uses(card, "stats"):
        checks.append("- Costs and harm. If someone casts magic, uses an ability or exerts themselves and no result above already charged for it, lower the stat that fuels it (about 1 to 3 for something small, 4 to 6 for something solid, 8 or more for something great, on a stat that runs to about 20; scale that to the numbers the game state shows for the stat). If someone is hurt or healed, change the stat that measures it by a fitting amount. Recovery counts too: when the text shows someone has slept, rested, eaten or let time pass, raise what that restores for each person it applies to. A full night's sleep, or days passing, brings back everything that is spent and regained, whether or not the text names it. For each person who slept, read their Stats line in the game state and, for every stat shown as two numbers with a slash (12/20), record a change_stat of the difference so it reaches the second number, which is that person's own maximum. That covers health, stamina, mana and any other such stat alike; nobody who slept the night is left below a maximum. A short rest or a meal brings back part. Leave out a stat that is already at its maximum." + (
            "\n  What each stat is: %s" % "; ".join("%s (%s): %s" % (stat["name"], stat["id"], stat["description"]) for stat in card.data["rules"].get("stats", []) if stat.get("description"))
            if any(stat.get("description") for stat in card.data["rules"].get("stats", [])) else ""))
    if uses(card, "states"):
        checks.append("- States, as [States] below describes them. First go through every state the game state lists on anyone: does it still hold at the end of the text? clear_state each one that has ended (they woke, landed, got free, came back, calmed down). A state that is over is removed with clear_state, never kept with a note saying it is over. Then set_state what has begun, but only what passes the test there: a condition that will last, of body, situation or mind. A single act is not recorded.")
    if uses(card, "map"):
        checks.append("- Places. Move someone only when the text shows them arrived somewhere else by its end. People talking about going somewhere, deciding to, being invited to, getting ready to, or setting off without the text showing them arrive are all still where the game state has them, and nothing is recorded; they will be moved when a later text shows them there. If the player ends the text somewhere other than the Location in the game state, move them there, however they got there and however far it is (a portal, a journey, being taken), including back to where they were if they were stopped from leaving. Move every character who went with them too. If they end up in a place that is not in the game state's lists at all, create_location it first when that action is listed above, then move them there by its name. Someone who turns up where the player is, acts there or is spoken to face to face is there, whether or not the text says how they came: move them to the player's location if the game state has them elsewhere. If the text makes plain they are now held in place, lock_travel; if it lets them go, unlock_travel.")
    if uses(card, "inventory") or uses(card, "money"):
        checks.append("- Belongings. Anything handed over, picked up, found, lost, broken, used up, paid or received. Only a change of hands counts: what someone is merely described as wearing, holding or working with is scenery, not a new item, and what is promised, offered, owed or still to be collected has not changed hands yet.")
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
    paid = ["- %s: %s" % (p["quest"], ", ".join(
                ["%s %s" % (_fmt(p["money"]), card.currency)] * bool(p["money"])
                + ["%s %s" % (_fmt(n), card.stats[s]["name"]) for s, n in p["stats"].items() if n > 0 and s in card.stats]
                + ["%s x%d" % (card.items[i]["name"], n) for i, n in p["items"].items() if n > 0 and i in card.items]))
            for p in paid_lately(state)]
    rewards = "[Quest rewards the game has already paid]\n%s\nIf the text shows one of these being handed over, announced or awarded, it is this same reward: record nothing for it.\n\n" % "\n".join(paid) if paid else ""
    ## clock is (the line before this text, the line that heads it), either of which may be missing.
    ## It is there to be read: how much time went by decides how much anyone recovered.
    before, now = clock or (None, None)
    times = "[Time and place, for reference]\n%s%sThe story keeps this itself. Use it to judge how much time has passed; record nothing for it.\n\n" % (
        "Before this text: %s\n" % before.strip("[] ") if before else "", "With this text: %s\n" % now.strip("[] ") if now else "") if before or now else ""
    already = "\n[Engine results already recorded this turn]\n%s\n" % _results_text(results) if results else ""
    earlier = "\n\n".join(t["narration"][-600:] for t in state["history"][-2:])
    user = "%s\n\n%s%s[Player's message]\n%s\n%s\n[Narrator's new text]\n%s" % (
        describe_state(card, state, focus="%s\n%s" % (player_text, narration)), describe_quests(card, state) + "\n\n" if quests and describe_quests(card, state) else "",
        rewards + times + ("[Just before, already recorded; for context only]\n%s\n\n" % earlier if earlier else ""), player_text, already, narration)
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
        if not isinstance(verdict, dict) or not isinstance(verdict.get("quest"), str) or verdict["quest"] not in card.quests:
            continue
        if verdict.get("verdict") == "done" and isinstance(verdict.get("objective"), str):
            actions.append({"type": "quest_advance", "quest": verdict["quest"], "stage": verdict["objective"]})
        elif verdict.get("verdict") == "failed":
            actions.append({"type": "quest_fail", "quest": verdict["quest"]})
    for quest in parsed.get("start") if isinstance(parsed.get("start"), list) else []:
        if isinstance(quest, dict):                 # some models answer {"quest": "id"} here too, the way verdicts are written
            quest = quest.get("quest", quest.get("id"))
        if isinstance(quest, str) and quest in card.quests:
            actions.append({"type": "quest_start", "quest": quest})
    return actions


def answers_with(text, key):
    """Whether a helper's reply is the JSON object it was asked for, with a list under key. A reply
    that is not must not be mistaken for "nothing to report"."""
    parsed = extract_json(text)
    return isinstance(parsed, dict) and isinstance(parsed.get(key), list)


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


# The time and place line: one bracketed line the story model puts at the head of each reply,
# saying the hour, the date, the exact spot and the weather. The model keeps it going from its own
# previous line; the game takes it off the story text, shows it apart, and hands it back with the
# reply it headed. A card can give its own form for the line, and the line the story starts on.
DEFAULT_HEADER = "[ 🕰️ HH:MM AM/PM | 🗓️ Day # - DayOfWeek, Month DD, YYYY Era | 📍 Location - Specific area | WeatherEmoji Weather, Temp °F ]"
_HEADER = re.compile(r"^\s*(\[[^\n]{6,400}\])[ \t]*(?:\n|$)")


def _bracketed(line):
    line = " ".join(line.split())
    return line if line.startswith("[") and line.endswith("]") else "[ %s ]" % line.strip("[] ")


def header_format(card):
    return _bracketed(card.data["world"].get("header_format") or "") if (card.data["world"].get("header_format") or "").strip() else DEFAULT_HEADER


def header_start(card, state):
    """The line the card's story begins on, or "" when the card leaves that to the story model."""
    line = (card.data["world"].get("header_start") or "").strip()
    return fill(card, state, _bracketed(line)) if line else ""


_HEADER_LINE = re.compile(r"^[ \t]*(\[[^\n]{6,400}\])[ \t]*$", re.M)
_UNFILLED = re.compile(r"HH:MM|DayOfWeek|WeatherEmoji|YYYY|Month DD|Day #")


def split_header(text):
    """(the time and place line or None, the story without it).

    Some models write the form out first, placeholders and all, and then the real line after a
    remark about correcting it. So if a filled-in line follows close behind an unfilled one, that
    is the line, and what came before it is dropped with it."""
    found = _HEADER.match(text)
    if not found or not ("|" in found.group(1) or re.search(r"\d", found.group(1))):
        return None, text
    if _UNFILLED.search(found.group(1)):
        later = [m for m in _HEADER_LINE.finditer(text[:found.end() + 700], found.end()) if "|" in m.group(1) and not _UNFILLED.search(m.group(1))]
        if later:
            found = later[0]
        else:
            return None, text[found.end():].lstrip("\n")       # only the form itself: no line this turn
    return " ".join(found.group(1).split()), text[found.end():].lstrip("\n")


# The clock is kept by three hands. The story model writes the line. The game reads it, and knows
# when it looks wrong: time that ran backwards, a clock that has not moved for several replies, a
# place that is not where the game has the player, or no line at all. Then, and only then, a helper
# job is asked to write the line as it should be, and to say where the player is.
#
# Looking wrong is not being wrong. The line is the story's time, and a story may hold time still
# or turn it back. Only a reader of the story can tell that from a slip, so the game never decides
# it: the helper does, and when it gives the line back as it stood, the game takes its word and
# does not ask again for as long as the clock stays at that moment (state["clock_held"]).
STUCK = 3       # turns the clock may stand at the same minute before it is taken to be stuck


def header_place(card, state, line):
    """The id of the known place the line puts the player in, or None when it names none the game
    has. The line gives the place and then the spot within it, so the name that comes first wins,
    and the longer of two that start together ("Hideout Kitchen" over "Hideout")."""
    said, found, loose = clock.place_part(line), [], re.I
    if said is None:
        # No pin marks the place, so the whole line is searched, and only for a name written as the
        # card writes it: "a stable door" in the weather is not the Stable.
        said, loose = line or "", 0
    for lid, place in places(card, state).items():
        name = place["name"].strip()
        for wanted in set((name, name[4:] if name.lower().startswith("the ") else name)):
            at = re.search(r"(?<!\w)%s(?!\w)" % re.escape(wanted), said, loose) if wanted else None
            if at:
                found.append((at.start(), -len(wanted), lid))
    return min(found)[2] if found else None


def clock_problems(card, state, before, now):
    """What is wrong with the line that heads the newest reply, as sentences for the timekeeper.
    Empty when nothing is, which is nearly always. before is the line the story stood at."""
    me = state["actors"][PLAYER]
    if not now:
        return ["The reply was given no time and place line. Write one, carried on from the line before."] if before else []
    problems = []
    if before and clock.went_back(before, now):
        problems.append("The time is earlier than in the line before.")
    elif not (state.get("clock_held") and clock.same_moment(state["clock_held"], now)):
        stood = 0
        for turn in reversed(state["history"]):
            if not (turn.get("header") and clock.same_moment(turn["header"], now)):
                break
            stood += 1
        if stood >= STUCK:
            problems.append("The clock has stood at the same minute for %d replies." % (stood + 1))
    at, here = header_place(card, state, now), places(card, state).get(me["location"])
    if at and here and at != here["id"]:
        problems.append("The line puts %s at %s, but the game has them at %s. Only one of the two can be right." % (me["name"], places(card, state)[at]["name"], here["name"]))
    return problems


def settle_clock(state, now, fixed):
    """Remembers that the timekeeper left the clock where the story model had it, so that a clock
    the story is holding still is not asked about again on every turn it stays there."""
    if now and fixed and clock.same_moment(now, fixed):
        state["clock_held"] = fixed
    else:
        state.pop("clock_held", None)


def timekeeper_prompt(card, state, before, now, problems, player_text, narration, prompts=None):
    me = state["actors"][PLAYER]
    here = places(card, state).get(me["location"])
    parts = []
    if card.locations:
        parts.append("[Places]\n%s\n%s is at %s, as the game has it." % (
            ", ".join(_named(l) for l in place_list(card, state)), me["name"], _named(here) if here else "a place off the map"))
    parts.append("[The form of the line]\n%s" % header_format(card).strip("[] "))
    if before:
        parts.append("[The line before]\n%s" % before.strip("[] "))
    if now:
        parts.append("[The line now]\n%s" % now.strip("[] "))
    parts.append("[What is wrong]\n%s" % "\n".join("- " + p for p in problems))
    parts.append("[The player's message]\n%s" % player_text)
    parts.append("[The reply]\n%s" % narration)
    return prompt_text(prompts, "keep_time"), [{"role": "user", "content": fill(card, state, "\n\n".join(parts))}]


def parse_timekeeper(text, card, state, before, now=None):
    """(the line as the timekeeper put it right, the id of the place it says the player is in).
    Either is None when the answer does not give it, or gives a line that would itself be wrong:
    the form written out unfilled, or a time earlier than the line before. An earlier time is
    taken only when it is the very time the story model wrote (now): the helper upholding the
    story, not a slip of its own."""
    parsed = extract_json(text)
    parsed = parsed if isinstance(parsed, dict) else {}
    line = parsed.get("line")
    line = _bracketed(line) if isinstance(line, str) and len(line.strip("[] ")) >= 6 and "\n" not in line.strip() else None
    if line and (_UNFILLED.search(line) or len(line) > 400 or (before and clock.went_back(before, line) and not (now and clock.same_moment(now, line)))):
        line = None
    at = parsed.get("player_at")
    return line, at if isinstance(at, str) and at in places(card, state) else None


def _when(turn, label="Time and place: %s\n"):
    """The time and place line a turn was given, for a helper to read. Helpers never write it."""
    return label % turn["header"].strip("[] ") if turn.get("header") else ""


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


# What each thing a state can stop means for the telling. The engine refuses the player's own game
# actions; the rest (and all of it for other characters) only holds if the story is told that way.
LIMIT_MEANS = {
    "all": "cannot act: nothing they try to do gets done",
    "move": "cannot go anywhere: trying is a struggle that leaves them where they are",
    "items": "cannot use or hand over what they carry",
    "equipment": "cannot put on or take off what they wear",
    "skills": "cannot use skills: an attempt comes to nothing",
    "trade": "cannot buy or sell",
    "attack": "cannot strike anyone",
    "speech": "cannot speak: whatever they try to say comes out as muffled or wordless sound, and nobody makes out the words",
    "sight": "cannot see: they know only what they hear, feel, smell or are told",
    "hearing": "cannot hear: what is said near them does not reach them as words",
}


# What the player types is what their character means to say and do. When a state has taken their
# voice, sight or hearing, the story model still reads every word of it, and left alone it has the
# others answer those words. So the turn's message carries a note saying how the message lands.
SENSE_NOTES = {
    "speech": "%(name)s cannot speak (%(state)s). What they say in this message is what they meant, not what was heard: it came out as muffled or wordless sound. "
              "Others respond only to what reached them, and may ask or guess, rightly or wrongly. Someone understands the meaning only where the story gives a real reason "
              "(a power, an agreed sign, knowing %(name)s closely%(bond)s), and then only the gist.",
    "sight": "%(name)s cannot see (%(state)s). Tell this turn through their other senses; whatever in their message depends on seeing is a guess.",
    "hearing": "%(name)s cannot hear (%(state)s). Speech does not reach them as words; whatever in their message answers something said aloud is a guess.",
}


def sense_notes(card, state, text=""):
    """Notes for this turn's message on how it lands, one for each of speech, sight and hearing that
    the player's states have taken. They travel with the engine results as things left to the story
    (ok is None), so the player is not shown them and the turn is kept as it was sent. text is the
    player's message; what they tried to say in it is given as the sound it made."""
    me = state["actors"][PLAYER]
    bond = ", or a high %s toward them" % card.relationship_name if uses(card, "relationships") else ""
    notes = []
    for what, name in limits(card, me):
        if what in SENSE_NOTES:
            said = muffled(text)[1] if what == "speech" else []
            notes.append({"action": None, "ok": None, "message": SENSE_NOTES[what] % {"name": me["name"], "state": name.lower(), "bond": bond} + (
                " What came out of them, to quote if you tell it: %s" % " ... ".join('"%s"' % s for s in said) if said else "")})
    return notes


def heard_as(card, state, text):
    """The player's message as the game shows it when they cannot speak: their words as the noise
    they made. None when they can speak, or when nothing in the message can be told to be speech."""
    if not any(what == "speech" for what, name in limits(card, state["actors"][PLAYER])):
        return None
    shown, said = muffled(text)
    return shown if said else None


def _state_parts(card, actor):
    """The states someone is in and, in plain words, what those states keep them from."""
    parts = ["State: %s" % describe_states(actor)]
    held = limits(card, actor)
    if held:
        parts.append("So they %s. This holds until something in the story ends it, which their own attempt can do only if they truly have the means" % "; ".join(
            "%s (%s)" % (LIMIT_MEANS[what], name.lower()) for what, name in held))
    return parts


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
        parts.extend(_state_parts(card, actor))
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


def witnesses(card, turn):
    """Ids of the characters who were there for a turn: with the player when it began or when it
    ended, or speaking in it. None for a turn played before the game kept track.

    This is what lets the story model tell what each character can know. It reads the whole story;
    a character was only there for part of it."""
    if "there" in turn:
        ids = turn["there"]
    elif "with" in turn:
        ids = sorted(set(turn["with"]) | set(d["speaker"] for d in turn.get("direction") or [] if d.get("speaker")))
    else:
        return None
    return [c for c in ids if c in card.characters]


def _present(card, turn, label="[Present: %s]"):
    """The line that heads a past turn, saying who was there for it."""
    ids = witnesses(card, turn)
    if ids is None or not card.characters:
        return ""
    return label % (", ".join(card.characters[c]["name"] for c in ids) or "nobody but {{user}}") + "\n"


RECENTLY = 12       # someone who joined longer ago than this many turns is no longer pointed out; the headings on the turns still say when


def _newcomers(card, state, present):
    """Of the characters in the scene, those who have not been there for the whole story so far,
    with how many of the latest turns they were there for: [(id, turns)]."""
    told = state["history"]
    late = []
    for who in present:
        run = 0
        for turn in reversed(told):
            there = witnesses(card, turn)
            if there is None:
                run = len(told)                     # before the game kept track: nothing can be said
                break
            if who not in there:
                break
            run += 1
        if run < min(len(told), RECENTLY):
            late.append((who, run))
    return late


def describe_scene(card, state, knowledge=False, hour=False):
    """A short briefing for the narrator on the scene as it stands: where it is, who is in it and
    what was going on. The facts are the engine's; the one line on what is happening is written by
    the scene director after each reply. It names only who is there. People who are somewhere else
    are not listed here, because a model tends to use whoever it is shown."""
    me = state["actors"][PLAYER]
    here = places(card, state).get(me["location"])
    present, ids = [], []
    for who, actor in sorted(state["actors"].items()):
        if who != PLAYER and actor["location"] is not None and actor["location"] == me["location"] and not is_away(card, actor):
            present.append(actor["name"] + (" (%s)" % actor["note"] if actor.get("note") else ""))
            ids.append(who)
    lines = ["[The scene right now]"]
    if here:
        lines.append("Place: %s." % here["name"])
    ## The hour, read by the game off the story's own line and said in plain words, where the story
    ## model is looking as it writes. A bare "18:13" at the head of an old reply is easy to lose.
    now = clock.hour_line(state.get("header") or header_start(card, state)) if hour else None
    if now:
        lines.append("Time: %s. Everyone in the scene knows the hour and the day and acts by them." % now)
    if card.characters:
        lines.append("With %s: %s" % (me["name"], "; ".join(present) + "." if present else "nobody else."))
    late = _newcomers(card, state, ids) if knowledge else []
    if late:
        lines.append("Here only since recently: %s. Each of them saw what happened from then on, and of anything earlier only the turns headed with their name." % "; ".join(
            "%s (%s)" % (state["actors"][who]["name"], "just arrived" if turns == 0 else "the last turn" if turns == 1 else "the last %d turns" % turns) for who, turns in late))
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
        parts.extend(_state_parts(card, actor))
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


def narrator_prompt(card, state, preset, player_text, results, record=True, check=False, header=False, recalled=()):
    """Returns (system, messages). One message carries "cache": True, marking the end of the part
    that will be identical next turn; llm.chat_request turns that into the provider's own marker.

    Text blocks above the history slot and every unchanging slot form the system text, in preset
    order. The newest message is built in three parts: the changing slots (state, quests, lore),
    then what the player said with the engine's results, then the text blocks placed below
    history, so that a reminder is the last thing read. A block's role is not used yet: all blocks
    are sent as instructions.
    """
    world = card.data["world"]
    own = (world.get("narrator_instructions") or "").strip()
    sent_own = [False]
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
        ## The card's own guidance is told apart from the preset's and given the last word. See wording.py.
        "narrator_instructions": lambda: prompt_text(preset.get("prompts"), "card_instructions", instructions=own) if own else "",
        "persona": lambda: "[The player's character]\nName: %s%s%s" % (
            me["name"], "\n" + me["description"] if me.get("description") else "",
            "\nAppearance: " + me["appearance"] if me.get("appearance") else ""),
        "characters": lambda: describe_cast(card, settled),
        "lorebook": lambda: describe_lore(card, _recent_text(card, state, player_text)),
        "state": lambda: describe_scene(card, state, knowledge=True, hour=header) + "\n\n" + describe_state(card, state, focus=in_play, cast=cast, items=record),
        "quests": lambda: describe_quests(card, state),
        ## The short running summary of everything that has left the prompt, then the journal entries that matter this turn.
        "summary": lambda: "\n\n".join(part for part in ("[Story so far]\n" + state["summary"] if state["summary"] else "",
                                                          journal.timeline(card, state),
                                                          journal.describe(journal.recall(card, state, in_play, recent=_recent_text(card, state, ""), picked=recalled), card)) if part),
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
        if text and slot == "narrator_instructions":
            sent_own[0] = True
        if text:
            ## The changing parts (state, quests, lore) go in front of what the player just said. An
            ## instruction placed below the story goes after it, as the very last thing the model
            ## reads before it writes: that is where a reminder is heeded most.
            (tail if slot in VOLATILE_SLOTS else closing if not slot and below_history else stable).append(text)
    if not has_protocol and action_protocol(card, record, preset.get("prompts")):
        stable.append(action_protocol(card, record, preset.get("prompts")))
    ## How to lay a reply out, so each paragraph can be shown under the right name. For every card.
    stable.append(prompt_text(preset.get("prompts"), "narrator_layout"))
    if card.characters:
        stable.append(prompt_text(preset.get("prompts"), "narrator_knowledge"))
    if header:
        stable.append(prompt_text(preset.get("prompts"), "narrator_header", format=header_format(card)))

    ## With the time and place line on, each reply is sent back headed by the line it was given, so
    ## the model carries the clock on from its own last line.
    headed = lambda line, text: "%s\n\n%s" % (line, text) if header and line else text
    messages = [{"role": "user", "content": OPENING_CUE}, {"role": "assistant", "content": headed(header_start(card, state), opening(card, state))}]
    for turn in live if history_on else []:
        ## Each finished turn is headed with who was there for it. See witnesses.
        messages.append({"role": "user", "content": _present(card, turn) + _entering(card, turn.get("cast")) + _turn_message(turn["player"], turn["results"])})
        messages.append({"role": "assistant", "content": headed(turn.get("header"), turn["narration"])})
    if check:
        ## For a model that thinks before answering: a checklist to go through first. See parse_narration for what happens to a check written out.
        closing.append(prompt_text(preset.get("prompts"), "narrator_check"))
    if sent_own[0]:
        closing.append(prompt_text(preset.get("prompts"), "card_reminder"))
    messages[-1]["cache"] = True
    messages.append({"role": "user", "content": "\n\n".join(tail + [_entering(card, entering) + _turn_message(player_text, results)] + closing)})

    return fill(card, state, "\n\n".join(stable)), [dict(m, content=fill(card, state, m["content"])) for m in messages]


_ACTIONS_BLOCK = re.compile(r"<actions>(.*?)</actions>", re.DOTALL | re.IGNORECASE)


# A model that copies the prompt's own layout into its reply: an "[Engine results]" heading and
# result lines it made up. Left in, the player would read it, and the bookkeeper would take the
# made-up line for something already recorded and record nothing.
_ECHOED_RESULTS = re.compile(r"^[ \t]*(\[Engine results[^\]\n]*\]|- (done|REJECTED|UP TO YOU)\b[^\n]*)[ \t]*\n?", re.M)


# What a model thought through before writing, when it wrote that out instead of keeping it to
# itself: the check it was asked for, or a reasoning model's thinking passed along in the reply.
_THOUGHTS = re.compile(r"<(check|think|thinking)>.*?</\1>\s*", re.DOTALL | re.IGNORECASE)
_THOUGHTS_UNCLOSED = re.compile(r"^\s*<(check|think|thinking)>.*?(\n\s*\n|$)", re.DOTALL | re.IGNORECASE)


def parse_narration(text):
    """Splits a narrator reply into (story text, actions). A block cut off mid-way is dropped."""
    text = _THOUGHTS_UNCLOSED.sub("", _THOUGHTS.sub("", text), count=1)
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
        if isinstance(action, dict) and isinstance(action.get("type"), str) and action["type"] in allowed:
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


SUMMARY_WORDS, SUMMARY_WORDS_ALONE = 350, 600       # the most words: beside a journal, and as the only memory
SUMMARY_STANDING = ("The narrator is shown, apart from your summary, a list of every scene that has happened, in order. So do not retell events: what happened is kept there and cannot be lost. "
                    "Yours is how things stand now. Something that happened belongs here only for what it left behind: a promise, a debt, a grudge, a secret, a changed footing between two people.")
SUMMARY_EVENTS = "Yours is the only record the narrator has of these scenes. Keep the events that matter as well, briefly and in the order they happened, with when they happened where the scenes say."


def summary_prompt(card, state, turns, prompts=None, listed=False):
    ## What the summary is for depends on whether the journal is keeping the events. With a journal,
    ## every scene has its own line in a list that is only added to, so the summary need not retell
    ## anything: it holds how things stand, which stays short however long the story runs, because
    ## what is settled leaves it. With no journal it is the only memory, and has to carry the events
    ## too, in more room.
    system = fill(card, state, prompt_text(prompts, "summarize", scope=SUMMARY_STANDING if listed else SUMMARY_EVENTS,
                                           length="At most %d words." % (SUMMARY_WORDS if listed else SUMMARY_WORDS_ALONE)))
    scenes = "\n\n".join("%s%sPlayer: %s\nNarrator: %s" % (_when(t), _present(card, t, "Present: %s"), t["player"], t["narration"]) for t in turns)
    user = "[Existing summary]\n%s\n\n[New scenes]\n%s" % (state["summary"] or "(none yet)", scenes)
    return system, [{"role": "user", "content": fill(card, state, user)}]


# The memory helper: picks, from the list of scenes the story model no longer sees, the ones this
# turn calls for. It reads the player's message alongside the helper that works out their actions,
# so it adds no wait. Words can only find a scene spoken of in its own words (see journal.recall).

def recall_prompt(card, state, player_text, prompts=None):
    scenes = "\n".join("%d. %s" % (entry["id"], journal.scene_line(card, entry)) for entry in journal.candidates(state))
    user = "[Earlier scenes]\n%s\n\n%s\n\n[The last reply]\n%s\n\n[The player's message]\n%s" % (
        scenes, describe_scene(card, state), last_narration(card, state)[-1500:], player_text)
    return prompt_text(prompts, "recall_memory"), [{"role": "user", "content": fill(card, state, user)}]


def parse_recall(text, state):
    """The ids of the entries the memory helper picked, in its order. Anything that is not the id
    of an entry is dropped, and an answer that cannot be read picks nothing."""
    parsed = extract_json(text or "")
    picked = parsed.get("scenes") if isinstance(parsed, dict) and isinstance(parsed.get("scenes"), list) else []
    known = set(entry["id"] for entry in journal.candidates(state))
    return [n for n in picked if isinstance(n, int) and not isinstance(n, bool) and n in known][:journal.POINTED]


def journal_prompt(card, state, turns, prompts=None):
    """Asks for a journal entry about one scene: the turns given, oldest first. See journal.py."""
    scene = "\n\n".join("%s%sPlayer: %s\nNarrator: %s" % (_when(t), _present(card, t, "Present: %s"), t["player"], t["narration"]) for t in turns)
    return fill(card, state, prompt_text(prompts, "write_journal")), [{"role": "user", "content": fill(card, state, "[Scene]\n%s" % scene)}]


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
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] not in card.characters:
            continue
        update = {"id": entry["id"]}
        if "location" in entry and (entry["location"] is None or entry["location"] == "here" or entry["location"] in known):
            update["location"] = entry["location"]
        if isinstance(entry.get("note"), str):
            update["note"] = entry["note"].strip()[:120]
        if len(update) > 1:
            updates.append(update)
    return updates


# The world moving on: a helper that decides where people have got to while the player was not
# with them. Without it everyone stays for ever where the story last showed them.

OFFSTAGE = 25       # the most people one call is asked about


def offstage(card, state, limit=OFFSTAGE):
    """Ids of the people the player knows who are not with them now: those the player is closest
    to first, then in the order they entered the story."""
    me = state["actors"][PLAYER]
    entered = state.get("cast", [])
    ids = [who for who, actor in state["actors"].items()
           if who != PLAYER and who in card.characters and actor.get("known", True) and not (actor["location"] is not None and actor["location"] == me["location"])]
    ids.sort(key=lambda who: (-state["actors"][who].get("relationship", 0), entered.index(who) if who in entered else len(entered), who))
    return ids[:limit]


def world_prompt(card, state, prompts=None):
    me = state["actors"][PLAYER]
    here = places(card, state).get(me["location"])
    people = []
    for who in offstage(card, state):
        c = card.characters[who]
        about = " ".join((c.get("description") or "").split())
        home = places(card, state).get(c.get("location"))
        people.append("- %s (id: %s). %sLast known: %s.%s" % (c["name"], who, about[:200].rstrip(".") + ". " if about else "", _whereabouts(card, state, who),
                                                               " Began the story at %s." % home["name"] if home else ""))
    latest = "\n\n".join("%s\n%s" % (t["player"], t["narration"][-700:]) for t in state["history"][-3:])
    user = "[Places]\n%s\n\n[The player]\nNow at %s.%s\n\n[People elsewhere]\n%s\n\n[Story so far]\n%s\n\n[Latest turns]\n%s" % (
        ", ".join(_named(l) for l in place_list(card, state)), _named(here) if here else "a place off the map", "\nTime and place now, as the story has it: %s" % state["header"].strip("[] ") if state.get("header") else "", "\n".join(people), state.get("summary") or "(nothing before the latest turns)", latest or "(the story has only just begun)")
    return prompt_text(prompts, "move_world"), [{"role": "user", "content": fill(card, state, user)}]


def parse_world(text, card, state):
    """Where the helper says people have got to, ready for state.track. Only people it was asked
    about, and never to where the player is: only the story brings someone to the player."""
    asked = set(offstage(card, state))
    here = state["actors"][PLAYER]["location"]
    return [u for u in parse_whereabouts(text, card, state) if u["id"] in asked and u.get("location", None) != "here" and not ("location" in u and u["location"] is not None and u["location"] == here)]


def credible_whereabouts(card, state, text, updates):
    """Drops what the scene director says about where someone went when the text gives it no way of
    knowing: a character the text does not name and who is not with the player. A model asked
    about everyone sometimes answers about everyone, and so sends off someone the player merely
    walked away from. Being told someone is "here" is always allowed: that is how an unnamed
    newcomer is recognised."""
    named = set(named_in(card, text))
    here = state["actors"][PLAYER]["location"]
    kept = []
    for update in updates:
        actor = state["actors"].get(update.get("id"))
        if actor is None:
            continue
        if "location" not in update or update["location"] == "here" or update["id"] in named or actor["location"] == here:
            kept.append(update)
    return kept


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
