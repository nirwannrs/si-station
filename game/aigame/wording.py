"""The built-in wording: every instruction the game itself writes for a model.

A preset's own instructions (its blocks) are the player's or the creator's. These are the game's:
how the rules are explained to the story model, and the whole prompt of each helper job. Each has a
default here and can be replaced by a preset, under "prompts", so the wording can be tuned for a
model without touching code. The game's Preset screen and the creator's preset builder both list
whatever is registered here, so a prompt added here is editable in both from that moment.

Any new helper job must register its prompt here under the job's own name. A test fails otherwise.

Parts written as {{name}} are filled in by the game (a list of actions that depends on the card,
say). If an edited prompt leaves one out, the part is added at the end, so nothing the model needs
can be lost by an edit.
"""

import json
import os
import re

BUILTIN_PROMPTS = []        # in the order the editors show them
_BY_KEY = {}


def _builtin(key, reader, title, help, parts, text):
    """reader is "story" for wording sent to the story model, "helper" for a helper job's prompt.
    parts maps each {{name}} the game fills in to a few words saying what it is."""
    entry = {"key": key, "reader": reader, "title": title, "help": help, "parts": parts, "text": text}
    BUILTIN_PROMPTS.append(entry)
    _BY_KEY[key] = entry


def _sends(key, where, *sections, **parts):
    """Records how a prompt goes out, for the editors to show beside it. where says how the wording
    itself is sent. sections are the headed parts the game sends under it, in order. parts are the
    {{name}} parts inside the wording. Each section and part says what it holds, where that comes
    from, and where it can be changed if anywhere ("card:SECTION" for a page of the card editor,
    "preset:PAGE" or "preset:PAGE/ENTRY" for the preset builder, None when it is the game's alone).
    A test compares the sections with what the prompt builders really send."""
    _BY_KEY[key]["where"] = where
    _BY_KEY[key]["sends"] = [{"heading": heading, "what": what, "source": source, "link": link} for heading, what, source, link in sections]
    _BY_KEY[key]["part_sources"] = dict((name, {"source": source, "link": link}) for name, (source, link) in parts.items())
    assert set(parts) == set(_BY_KEY[key]["parts"]), key


def builtin_prompt(key):
    return _BY_KEY[key]


def prompt_text(prompts, key, **parts):
    """The wording for one built-in prompt: the preset's own if it has one, else the default, with
    the game's parts filled in. prompts is a preset's "prompts" (or None)."""
    entry = _BY_KEY[key]
    text = (prompts or {}).get(key)
    if not isinstance(text, str) or not text.strip():
        text = entry["text"]
    for name in entry["parts"]:
        token, value = "{{%s}}" % name, parts.get(name, "")
        if token in text:
            text = text.replace(token, value)
        elif value.strip():
            text = text.rstrip() + "\n\n" + value.strip()
    return text


def check_preset(preset):
    """Problems with a preset, in plain words. Empty when the game can use it."""
    if not isinstance(preset, dict):
        return ["a preset must be a JSON object"]
    problems = []
    if preset.get("spec") != "aigame-preset":
        problems.append('spec must be "aigame-preset"')
    if not isinstance(preset.get("name"), str) or not preset["name"].strip():
        problems.append("the preset needs a name")
    blocks = preset.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return problems + ["the preset needs at least one block"]
    seen = set()
    for index, block in enumerate(blocks):
        where = "block %d" % (index + 1)
        if not isinstance(block, dict) or not isinstance(block.get("id"), str) or not block["id"]:
            problems.append("%s needs an id" % where)
            continue
        where = "block %s" % block["id"]
        if block["id"] in seen:
            problems.append("two blocks share the id %s" % block["id"])
        seen.add(block["id"])
        if block.get("kind") == "text":
            if not isinstance(block.get("content"), str):
                problems.append("%s needs its text" % where)
        elif block.get("kind") == "slot":
            if block.get("slot") not in SLOTS:
                problems.append("%s names an unknown built-in part %r" % (where, block.get("slot")))
        else:
            problems.append('%s must be of kind "text" or "slot"' % where)
    if not any(isinstance(b, dict) and b.get("slot") == "history" for b in blocks):
        problems.append("the preset needs the Recent turns (history) part, or the model never sees the story")
    prompts = preset.get("prompts", {})
    if not isinstance(prompts, dict):
        problems.append("prompts must be an object")
    else:
        for key, text in prompts.items():
            if key not in _BY_KEY:
                problems.append("prompts has an unknown entry %r" % key)
            elif not isinstance(text, str):
                problems.append("prompts.%s must be text" % key)
    for group, keys in (("sampling", ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "max_tokens")),
                        ("context", ("max_context_tokens", "summarize_after_turns"))):
        values = preset.get(group, {})
        if not isinstance(values, dict):
            problems.append("%s must be an object" % group)
            continue
        for key, value in values.items():
            if key not in keys:
                problems.append("%s has an unknown setting %r" % (group, key))
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append("%s.%s must be a number" % (group, key))
    return problems


PRESET_ENDING = ".preset.json"


class PresetError(Exception):
    """Carries a message that is fit to show to the player."""


def import_preset(path, folder):
    """Copies a preset file someone shared into the presets folder and returns the name it has
    there (its file name without the ending). The file is checked first; one the game could not
    use is refused with the reason. A preset is plain text and holds no keys or other secrets."""
    try:
        with open(path, "rb") as f:
            raw = f.read(2 * 1024 * 1024 + 1)
    except (IOError, OSError) as e:
        raise PresetError("The file could not be read: %s" % e)
    return take_preset(raw, os.path.basename(path), folder)


def take_preset(raw, file_name, folder):
    """The same, for a preset already read into memory (an upload), named after the file it came from."""
    if len(raw) > 2 * 1024 * 1024:
        raise PresetError("That file is too large to be a preset.")
    try:
        preset = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PresetError("That file is not a preset: it is not valid JSON.")
    problems = check_preset(preset)
    if problems:
        raise PresetError("That file is not a usable preset: %s." % problems[0])
    base = os.path.basename(file_name or "")
    for ending in (PRESET_ENDING, ".json"):
        if base.lower().endswith(ending):
            base = base[:-len(ending)]
            break
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or re.sub(r"[^a-z0-9]+", "_", preset["name"].lower()).strip("_") or "preset"
    if base == "default":
        base = "imported_default"       # the game's own preset is never replaced
    name, n = base, 2
    while os.path.exists(os.path.join(folder, name + PRESET_ENDING)):
        name, n = "%s_%d" % (base, n), n + 1
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with open(os.path.join(folder, name + PRESET_ENDING), "wb") as f:
        f.write((json.dumps(preset, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return name


# The built-in parts a preset's slot blocks can name. The game fills them from the card and the save.
SLOTS = ("world", "narrator_instructions", "persona", "characters", "lorebook", "state", "quests", "summary", "history", "action_protocol")


_builtin('narrator_mechanics', 'story', "How the game's rules work", "Opens the Game mechanics part of the story model's instructions: the engine is the source of truth, and what done, REJECTED and UP TO YOU mean.",
         {'tracked': "the list of things this card's engine tracks"}, """\
[Game mechanics]
A game engine tracks {{tracked}}. It is the source of truth; the state shown to you is exact.

The player's message may come with engine results for things they tried to do. Treat them as fact:
- "done" happened. Narrate it.
- "REJECTED" did not happen. Narrate the attempt failing for the stated reason, in the story's voice (reaching for a pouch that is empty, a door that will not open). Never narrate a rejected action as succeeding.
- "UP TO YOU" is something the engine left to the story, such as setting off for a place that is not next door. Decide what happens and narrate it: they may arrive by whatever means the story offers, be delayed on the way, or be unable to go.
Never describe {{user}} gaining, losing or using something the engine tracks unless a result or the state says so.
""")


_builtin('narrator_prose', 'story', "Writing for the bookkeeper", "Follows it when the bookkeeper is on: the story model only writes the story, clearly enough for the bookkeeper to record it.",
         {}, """\
You do not record changes yourself. A bookkeeper reads what you write and updates the game from it, so write so that what happened cannot be mistaken:
- Say outright who gives what to whom, what is used up or spent, who is hurt and roughly how badly, who arrives and who leaves, and where {{user}} ends up.
- When something that has been going on stops, say so in the story: {{user}} lands and is no longer flying, the ropes are cut, the sleeper wakes. The state shown to you stays as it is until your text ends it.
- Keep to the state: {{user}} cannot spend what they do not have or be somewhere the state does not put them.
Write only the story. No lists of changes, no notes to the bookkeeper.""")


_builtin('narrator_records', 'story', "Reporting changes itself", "Follows it when the bookkeeper is off: the story model lists what changed in an <actions> block.",
         {'actions': 'the actions this card allows the story model'}, """\
When events you narrate change the tracked state, end your reply with one block:
<actions>{"actions": [ ... ]}</actions>
Use only these, with ids from the state. WHO is "player" or a character id.
{{actions}}
Report only what has actually happened in your text, not what is promised, planned or talked about. Do not repeat anything already listed in the engine results, and when a finished quest's reward listed there is later handed over in the story, that is the same reward: do not give it again. Leave the block out when nothing changes. The player never sees it.""")


_builtin('narrator_layout', 'story', "How a reply is laid out", "Has the story model keep what a character does and what they say together in one paragraph, so the game can show it under that character's name.",
         {}, """\
[Layout of a reply]
The game shows each paragraph of your reply under the name of the character it belongs to, so lay the reply out by whose moment it is:
- A character's moment is one paragraph: what they do and what they say, together. Open it with the character by name, then their action, then their words in double quotes: Mira sets the ledger down and looks you over. "Room's four gold."
- Keep a character's action and their words in the same paragraph, also when they do something between two lines of speech.
- Start a new paragraph when another character takes over.
- What belongs to no one character (the place, the weather, what {{user}} sees or feels, several people acting at once) gets a paragraph of its own, without anyone's speech in it.""")


_builtin('narrator_check', 'story', "Check before writing", "A checklist the story model goes through in its thinking before it writes. Only sent when the player has switched it on, which is meant for models that think before answering.",
         {}, """\
[Before you write]
Plan this reply and check the plan before you write a word of the story. Do it in your private thinking. If you have no private thinking, write the check first inside <check> and </check>, which the game removes. None of it may show in the story itself.
Go through these, briefly:
1. The game state. Where {{user}} is, who is actually there, what each of them has, can do and is in the middle of. Does the plan fit it? Nobody who is elsewhere speaks or acts here, and nothing is used, spent or known that the state does not give them.
2. The engine results for this turn. Is each one shown in the story as it happened, and is anything rejected told as not having worked?
3. The instructions. Which of the instructions you were given apply to this reply: this story's own instructions first, then the rest (who you may speak for, person and tense, length, how a reply ends)? Does the plan follow each one?
4. The layout. Is each character's moment one paragraph, opening with their name, with their action and their words together?
5. The thread. Does it follow from the last reply and from what {{user}} just did, and agree with what is remembered from earlier?
Put right whatever fails. Then write only the story.""")


_builtin('card_instructions', 'story', "The card's own instructions come first", "Wraps the card's Instructions for the narrator, telling the story model to follow them over the preset's instructions wherever the two differ.",
         {'instructions': "the card's Instructions for the narrator"}, """\
[This story's own instructions]
The author of this story wrote what follows for you, for this story in particular. It comes first. Wherever it differs from anything else you are told here about how to write, what to include or leave out, tone, pacing, length or how the characters behave, follow this. Only the game's own rules, about what the game tracks and decides, stand above it.

{{instructions}}""")


_builtin('card_reminder', 'story', "Reminder of the card's instructions", "One line sent as the very last thing each turn, after the preset's own reminders. Only sent for a card that has Instructions for the narrator.",
         {}, """\
Above all, write this reply the way [This story's own instructions] ask. Where they and any other instruction pull in different directions, they win.""")


_builtin('leaving_prose', 'story', "When the player leaves a place (bookkeeper on)", "What walking out of a scene means. Only sent for cards with a map.",
         {}, """\
[When {{user}} leaves a place]
A result saying {{user}} left one place for another means they walked out of the scene that was going on. Do not carry that scene on as if they were still in it. Either someone there stops them, in which case say plainly that they are stopped and still where they were, or they are gone, in which case narrate where they are now and let what they walked out on have its consequences.""")


_builtin('leaving_records', 'story', "When the player leaves a place (bookkeeper off)", "The same, with the actions the story model uses to send the player back or lock the map.",
         {}, """\
[When {{user}} leaves a place]
A result saying {{user}} left one place for another means they walked out of the scene that was going on. Do not carry that scene on as if they were still in it. Decide what leaving means and narrate that:
- If someone there would have stopped them (a guard, a captor, an examiner who has not dismissed them), narrate them being stopped and put them back with a move action for "player" to the place they left. If they would be stopped every time, also use lock_travel so they cannot simply walk off again.
- Otherwise they are gone, and the place they left goes on without them. Narrate where they are now. Let what they walked out on have its consequences: people they left react when next met, and a quest that needed them there fails (quest_fail) if its "Fails if" says so or if leaving plainly makes it impossible.
You can also use lock_travel ahead of time, the moment a scene begins that {{user}} could not walk out of. While travel is locked the game state says so. It stays locked until you use unlock_travel, so do that as soon as the story lets them go: when the scene ends, they are dismissed, they escape or talk their way out. Never leave it locked once nothing is holding them.""")


_builtin('resolve_actions', 'helper', "Understand what the player does", "Reads the player's message and lists the game actions they are attempting.",
         {'actions': 'the actions this card allows the player'}, """\
You are the rules clerk of a text adventure. Read the player's message and list the game actions the player's character carries out in it. Most messages are talk and carry out none.

Only these actions exist for the player:
{{actions}}

Rules:
- Use ids from the game state. If the player names an item that is not in the state at all, use the player's own words as the item value; the engine will reject it.
- List a real attempt even when it looks impossible (the item is not in the inventory, there is not enough money). The engine decides. Never drop or correct an attempt.
- Speech, looking around, picking things up from the scene and anything else outside the list above produce no actions; the narrator handles them.
- An action is listed only when the message says the player's character does it, now. Before listing one, check that the message is not merely talking about it. All of these are talk and produce no actions: a plan or intention ("I'll drink it once we're inside", "let's go to the stable later"), a suggestion or question ("shall we head out?", "should I use the potion?"), a condition ("if he attacks, I'll draw my sword"), an offer waiting for an answer ("want me to patch that up?"), a mention or a showing ("I show her the letter", "I still have that draught"), something in the past ("I bought this in the capital"), and anything said inside the character's speech that is not also done.
- When it is unclear whether the player did it or only spoke of it, list nothing. The player can say so plainly next time; an action taken by mistake cannot be taken back.
- Do not guess at things the player did not say.

Reply with JSON only: {"actions": [ ... ]}. Use {"actions": []} when nothing applies.""")


_builtin('record_changes', 'helper', "Keep the books", "Reads each reply and records what it changed: stats, items, places, states.",
         {'actions': 'the actions this card allows', 'checks': 'the list of things to check, one for each system the card uses', 'states': 'what each state means'}, """\
You are the bookkeeper of a text adventure. A narrator has just written the next part of the story. Your job is to record what that text changed, so the game stays true to the story. The narrator records nothing; if you miss a change, the game is wrong from then on.

Read the new text against the game state and list every change it shows, using only these actions, with ids taken from the game state. WHO is "player" or a character id.
{{actions}}

Go through these every time:
{{checks}}

Rules:
- Record only what the text shows has actually happened by its end. What is promised, offered, planned, expected, threatened, remembered or only talked about has not happened: record nothing for it, and it will be recorded when a later text shows it happen.
- The engine results listed for this turn are already recorded. Do not record them again.
- Use the exact ids from the game state. If the text names something that has no id, use the closest action that fits, or leave it out.
- When nothing changed, the list is empty. That is a normal answer.{{states}}

Reply with JSON only: {"actions": [ ... ]}""")


_builtin('judge_quests', 'helper', "Judge quest progress", "Decides whether an objective is finished, for objectives the game cannot check itself.",
         {}, """\
You are the quest judge of a text adventure. Decide, strictly, whether each quest in progress has moved on. You are given every such quest's current objective and the recent story, ending with the newest text.

Give one verdict for each quest in progress:
- "done": the current objective is completely finished in the story. Every part of it has happened and is over. If it has several parts (several tests, several rooms, several opponents), all of them are over. If it gives a "Finished only when", that has plainly happened.
- "failed": only if the quest gives a "Fails if" and that has happened, or the story has made the quest impossible.
- "not_yet": everything else, including an objective that has started, is going well, or is nearly over. This is the usual answer.

When unsure, answer not_yet. Nothing is lost by moving on a turn later; moving on early skips part of the story.
Judge only each quest's current objective, the one named with it. Later objectives do not exist for you.

Under "start", list any quest from "Not started" that the newest text has clearly given to the player or set in motion. Otherwise leave it empty.

Reply with JSON only. Give the reason before the verdict:
{"verdicts": [{"quest": "quest_id", "objective": "objective_id", "why": "one short sentence", "verdict": "not_yet"}], "start": []}""")


_builtin('direct_scene', 'helper', "Direct the scene", "Picks who speaks and with what expression, tracks where characters are, and writes the line on what is going on.",
         {}, """\
You are the stage director of a visual novel. The narrator's latest text is given as numbered paragraphs. Work out four things from it.

1. Who speaks. For each paragraph:
- speaker: the id of the character the paragraph belongs to: the one who speaks in it, together with whatever that same character does around their words. A paragraph of only one character's actions belongs to them too when their own speech follows directly in the next paragraph. Use null for description that belongs to no one character, for a paragraph in which several characters speak, and for the player's own speech or actions.
- expression: how that speaker looks while saying it, chosen only from that character's listed expressions. Use null when speaker is null.

2. Where the characters are now. For each character whose place or activity the text changes or shows, give:
- location: "here" if they end the text in the player's company; a location id if they have arrived at or are at that place by the end of the text; null if they left for somewhere that is not on the list or are otherwise out of reach. Someone who only talks of going somewhere, agrees to, or is about to, has not gone: leave their location out.
- note: a few words on what they are doing or where they went, such as "tending the bar" or "rode off toward the capital".
Report only what the text states or plainly implies. Leave out characters the text does not mention. A character who arrives or is first met is "here".

The text does not always use names. Work out who an unnamed person is ("a skinny boy", "the woman behind the bar") from each character's listed looks and from where they were before this text. If you cannot tell which character someone is, leave them out of both lists rather than guess.

3. Which hidden places the player just learned of. If a list of places the player does not know of is given, name the ids of any that this text shows or tells the player about: they are told it exists, see the way to it, or are taken there. A place that is merely near is not revealed.

4. What is going on. Under "scene", one or two plain sentences on how things stand at the end of this text: who is with the player and doing what, and what is left hanging. Use names. It is a note for the narrator's next reply, so state facts from the text and add nothing.

Reply with JSON only:
{"paragraphs": [{"n": 1, "speaker": "some_id", "expression": "neutral"}, {"n": 2, "speaker": null, "expression": null}],
 "whereabouts": [{"id": "some_id", "location": "here", "note": "..."}],
 "revealed": ["some_location_id"],
 "scene": "..."}""")


_builtin('write_journal', 'helper', "Keep the journal", "Writes a short entry about a scene once it is over, so the story model can be reminded of it much later.",
         {}, """\
You keep the journal of a text adventure. A scene has just ended; it is given to you in full. Write one entry about it, so that the narrator can be reminded of this scene much later, when the scene itself is long out of sight.

Write what a storyteller would need to pick the thread up again: what happened and how it ended, what was decided or promised, what was learned or kept secret, how anyone's standing with the player changed, and what was left unfinished. Use names. Past tense, plain statements, at most 90 words. Leave out health, money and items; the game tracks those.

Give it a short title, and three to six keywords: the names, places, objects and subjects that, if they came up again, should bring this scene to mind. Use the words the story used.

Reply with JSON only:
{"title": "...", "content": "...", "keywords": ["...", "..."]}""")

_builtin('suggest_choices', 'helper', "Suggest replies", "Writes the suggested replies shown above the input box.",
         {'count': 'how many suggestions to give'}, """\
You suggest what the player could do next in a text adventure. Give {{count}} short, distinct options, written in first person as the player ("I ask Mira about the cellar."). Mix talking, acting and exploring. Only suggest using or buying things the game state shows are available. One sentence each.

Reply with JSON only: {"choices": ["...", "..."]}""")


_builtin('summarize', 'helper', "Summarize old turns", "Folds older turns into the running summary.",
         {}, """\
You keep the running summary of a text adventure so the narrator can remember earlier events. Merge the existing summary with the new scenes into one summary in past tense. Keep names, promises, secrets learned, relationships and unresolved threads. Leave out inventory, money and stats; the game tracks those. At most 250 words. Reply with the summary only.""")


# How each one is sent. A helper job is one call: its wording as the instruction, then a single
# message made of the headed sections listed here.

_HELPER = "Sent as the instruction (the system prompt) of its own call. One message follows it, made of the sections below."
_STORY = "Part of the story model's instructions, where the preset's instruction list has Game mechanics."

_STATE = ("The game writes it fresh for every call, from the save (where everyone is, what they hold, their numbers) and the card (names, items, places).", None)
_ACTIONS = ("A list the game keeps: one line for each action the engine accepts, with a few words on when to use it. Only the lines for systems this card uses are sent. It is fixed so that what the model is taught always matches what the engine can do.", None)

_sends("narrator_mechanics", _STORY + " It comes first, under the heading [Game mechanics].",
       tracked=("The systems this card uses, named in a row: fights, inventory, money, stats, locations, quests and so on. Decided by the card's Game setup.", "card:setup"))
_sends("narrator_prose", _STORY + " It follows How the game's rules work when the player has the bookkeeper on.")
_sends("narrator_records", _STORY + " It follows How the game's rules work when the player has the bookkeeper off.", actions=_ACTIONS)
_sends("narrator_layout", "Part of the story model's instructions, after the rest of the game's rules. Sent for every card.")
_sends("narrator_check", "Near the end of the newest message, after what the player said and the preset's own reminders. Only when the player has switched on Check before writing.")
_sends("card_instructions", "Part of the story model's instructions, where the preset's instruction list has the card's instructions. Only for a card that has them.",
       instructions=("What the card's author wrote under Instructions for the narrator.", "card:world"))
_sends("card_reminder", "The last lines of the newest message, after what the player said and after the preset's own reminders. Only for a card that has Instructions for the narrator.")
_sends("leaving_prose", _STORY + " It comes after Writing for the bookkeeper, for cards with a map.")
_sends("leaving_records", _STORY + " It comes after Reporting changes itself, for cards with a map.")
_sends("resolve_actions", _HELPER,
       ("[Current game state]", "where the player is, what they have and who is there; hidden places left out") + _STATE,
       ("[Last narration]", "the story model's previous reply", "The save: the reply the story model wrote last turn, word for word. On the first turn, the card's opening.", None),
       ("[Player message]", "what the player just typed", "The player, this turn.", None),
       actions=_ACTIONS)
_sends("record_changes", _HELPER,
       ("[Current game state]", "the full state, after the player's own actions were applied") + _STATE,
       ("[Active quests]", "open quests and objectives; only when no quest judge is deciding them", "The card's quests, and the save for how far each has got.", "card:quests"),
       ("[Quest rewards the game has already paid]", "the reward of a quest finished in the last few turns, so the story handing it over is not recorded as a second one; only when there is one", "The game: it pays a quest's reward the moment the quest is finished. The rewards are the card's.", "card:quests"),
       ("[Just before, already recorded; for context only]", "the end of the last two replies", "The save: the last 600 characters of each of the two replies before this one.", None),
       ("[Player's message]", "what the player just typed", "The player, this turn.", None),
       ("[Engine results already recorded this turn]", "what the game already applied, so it is not recorded twice; only when there is any", "The game: what it did with the player's own actions this turn, after Understand what the player does listed them.", "preset:helpers/resolve_actions"),
       ("[Narrator's new text]", "the reply to read and record", "The story model's reply this turn.", "preset:instructions"),
       actions=_ACTIONS,
       checks=("A list the game keeps: one line of things to look for, for each system this card uses (costs and harm, states, places, belongings, experience, relationships, fights).", None),
       states=("What each state means and what it stops. The states and their descriptions are the card's; the built-in ones (asleep, unconscious, restrained, away) are the game's.", "card:states"))
_sends("judge_quests", _HELPER,
       ("[Quests in progress]", "each open quest's current objective and what finishes it", "The card's quests: the objective's text and its \"It is finished only when\". The save says which objective each quest is on.", "card:quests"),
       ("[Not started]", "quests the story could still hand out", "The card's quests that have not begun.", "card:quests"),
       ("[Recent story, oldest first]", "the last five turns", "The save: what the player typed and the end of what the story model replied, for the five turns before this one.", None),
       ("[Newest]", "the player's message and the reply just written", "The player and the story model, this turn.", None))
_sends("direct_scene", _HELPER,
       ("[Characters]", "each character's looks, expressions and where they were before this text", "The card's characters (appearance, and one expression per sprite), and the save for where each one is.", "card:characters"),
       ("[Locations]", "where the player is, and the places that exist; only for cards with a map", "The card's locations, plus any the story has made, and the save for where the player is.", "card:locations"),
       ("[Places the player does not know of yet]", "hidden places the text might reveal; only when there are any", "The card's locations marked hidden that the player has not found.", "card:locations"),
       ("[Paragraphs]", "the reply, numbered paragraph by paragraph", "The story model's reply this turn, split at blank lines.", None))
_sends("suggest_choices", _HELPER,
       ("[Current game state]", "where the player is, what they have and who is there; hidden places left out") + _STATE,
       ("[Active quests]", "open quests, as the player sees them; only when there are any", "The card's quests, and the save for how far each has got.", "card:quests"),
       ("[Last narration]", "the reply the player is answering", "The story model's reply this turn.", None),
       count=("How many suggestions to ask for. Set in this preset, under About and starting values.", "preset:settings"))
_sends("summarize", _HELPER,
       ("[Existing summary]", "the summary so far", "The save: what this same job wrote the last time it ran. \"(none yet)\" the first time.", None),
       ("[New scenes]", "the turns being folded into it", "The save: the oldest turns that are not in the summary yet, word for word, each as what the player typed and what the story model replied. The game takes the older half of the turns the story model still sees, once there are more than the player's \"summarize after\" number or the prompt no longer fits the context size.", "preset:settings"))
_sends("write_journal", _HELPER,
       ("[Scene]", "the scene that just ended, turn by turn", "The save: every turn since the last journal entry, word for word, each as what the player typed and what the story model replied. The game decides where a scene ends: the player goes somewhere else, an objective is finished, a fight is over, or twenty turns have passed.", None))
