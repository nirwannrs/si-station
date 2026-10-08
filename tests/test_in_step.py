"""Checks that the parts of the project which must agree with each other still do.

The engine, the card format file that the creator and the MCP server hand out, the creator's
editor and the MCP tools are separate pieces. When one learns something new and another does not,
nothing crashes: a good card is just called broken, or a feature is silently missing. These tests
fail instead.

Run with: python3 -m unittest discover tests
"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))
sys.path.insert(0, os.path.join(ROOT, "creator"))

import mcp_server  # noqa: E402
from aigame import actions, card, prompt, wording  # noqa: E402
from templates import RULES  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


CARD_SCHEMA = json.loads(read("spec", "card.schema.json"))
ACTION_SCHEMA = json.loads(read("spec", "actions.schema.json"))


class InStepTest(unittest.TestCase):
    def test_card_format_knows_every_switch_the_engine_has(self):
        self.assertEqual(sorted(CARD_SCHEMA["properties"]["rules"]["properties"]["features"]["properties"]), sorted(card.FEATURES))
        self.assertEqual(CARD_SCHEMA["properties"]["states"]["items"]["properties"]["blocks"]["items"]["enum"], ["all"] + list(card.CAPABILITIES))
        self.assertEqual(tuple(CARD_SCHEMA["$defs"]["slot"]["enum"]), card.SLOTS)
        self.assertEqual(tuple(CARD_SCHEMA["properties"]["items"]["items"]["properties"]["type"]["enum"]), card.ITEM_TYPES)

    def test_every_action_the_engine_applies_is_in_the_action_format(self):
        described = sorted(option["properties"]["type"]["const"] for option in ACTION_SCHEMA["properties"]["actions"]["items"]["oneOf"])
        self.assertEqual(described, sorted(actions._HANDLERS))

    def test_the_model_is_only_taught_actions_that_exist(self):
        taught = set(kind for kind, need, line in prompt.PLAYER_ACTIONS)
        taught |= set(re.search(r'"type": "(\w+)"', line).group(1) for need, line in prompt.NARRATOR_ACTIONS)
        self.assertEqual(taught - set(actions._HANDLERS), set())
        self.assertEqual(set(actions.REQUIRES) - set(actions._HANDLERS), set())
        self.assertEqual(set(actions.NEEDS) - set(actions._HANDLERS), set())
        for feature in set(actions.REQUIRES.values()):
            self.assertIn(feature, card.FEATURES)

    def test_presets_and_samples_pass_the_engine(self):
        for name, rules in RULES.items():
            self.assertEqual(set(rules.get("features", {})) - set(card.FEATURES), set(), name)
        for name in sorted(os.listdir(os.path.join(ROOT, "cards"))):
            path = os.path.join(ROOT, "cards", name, "card.json")
            if os.path.isfile(path):
                self.assertEqual(card.check_card(json.loads(read("cards", name, "card.json"))), [], name)

    def test_the_editor_knows_every_switch_and_state_block(self):
        page = read("creator", "static", "app.js")
        defaults = re.search(r"const FEATURE_DEFAULTS = \{(.*?)\};", page).group(1)
        self.assertEqual(dict((k, v == "true") for k, v in re.findall(r"(\w+): (true|false)", defaults)), card.FEATURES)
        names = re.search(r"const CAPABILITY_NAMES = \{(.*?)\};", page).group(1)
        self.assertEqual(sorted(re.findall(r"(\w+): \"", names)), sorted(card.CAPABILITIES + ("all",)))
        for feature in card.FEATURES:
            self.assertIn('["%s", "' % feature, page, "no switch in Game setup for " + feature)

    def test_everything_the_creator_can_do_the_mcp_server_can_too(self):
        """Each thing the creator's server does, and the MCP tool that does the same. A new creator
        route fails this test until it is listed here with its tool, or with None and a reason."""
        same_as = {
            "templates": "create_card",                  # the starting points are offered by create_card's template choice
            "projects": "list_cards",                    # also create_card; opening and saving one are get_card, edit_card, replace_card
            "import sillytavern": None,                  # not offered: a whole-character import is a one-off done in the editor
            "convert lorebook": "import_lorebook",
            "asset": "add_image",
            "pack": "pack_card",
            "file": None,                                # only serves pictures to the editor page
            "presets": "save_preset",                    # also list_presets, get_preset, trash_preset; preset_format is the builder's reference
            "presets import": "save_preset",             # an assistant reads the file itself and saves its content
            "preview": None,                             # only shows the preset builder what a prompt looks like when sent
            "exports": "pack_card",                      # the download link; pack_card returns the file's path instead
        }
        source = read("creator", "server.py")
        routes = set(" ".join(json.loads("[%s]" % found)) for found in re.findall(r"(?:parts|rest|rest\[:1\]) == \[([^\]]+)\]", source))
        routes |= set(re.findall(r'parts\[0\] == "(\w+)"', source)) - {"projects"}
        self.assertGreaterEqual(len(routes), 8, "the route scan found too little; it no longer matches how server.py is written")
        self.assertEqual(routes - set(same_as), set(), "creator routes with no decision about the MCP server")
        tools = [t["name"] for t in mcp_server.TOOLS]
        for route, tool in same_as.items():
            if tool:
                self.assertIn(tool, tools, route)
        for tool in tools:
            self.assertTrue(callable(getattr(mcp_server.Tools, tool, None)), tool)
        self.assertEqual(mcp_server.Tools(None).card_format({})["schema"], CARD_SCHEMA)      # served from the file, never a copy


class EditablePromptsTest(unittest.TestCase):
    """Every instruction the game writes for a model can be reworded from a preset, in the game
    and in the creator. A helper job added without registering its prompt fails here."""

    def setUp(self):
        from aigame.card import load_card
        from aigame.state import new_game
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        self.preset = json.loads(read("presets", "default.preset.json"))

    def systems(self, prompts):
        c, s = self.card, self.state
        built = {
            "resolve_actions": prompt.resolver_prompt(c, s, "I wait.", prompts=prompts),
            "suggest_choices": prompt.suggest_prompt(c, s, 3, prompts=prompts),
            "summarize": prompt.summary_prompt(c, s, [], prompts=prompts),
            "direct_scene": prompt.director_prompt(c, s, ["Rain."], prompts=prompts),
            "record_changes": prompt.bookkeeper_prompt(c, s, "I wait.", [], "Rain.", prompts=prompts),
            "judge_quests": prompt.judge_prompt(c, s, "I wait.", "Rain.", prompts=prompts),
            "write_journal": prompt.journal_prompt(c, s, [{"player": "hi", "narration": "Rain."}], prompts=prompts),
            "move_world": prompt.world_prompt(c, s, prompts=prompts),
            "keep_time": prompt.timekeeper_prompt(c, s, "[ 9:00 PM | Day 1 ]", "[ 8:00 PM | Day 1 ]", ["It went backwards."], "I wait.", "Rain.", prompts=prompts),
            "recall_memory": prompt.recall_prompt(c, s, "I wait.", prompts=prompts),
        }
        return dict((task, system) for task, (system, messages) in built.items())

    def test_every_helper_job_has_a_prompt_that_can_be_reworded(self):
        tasks = [name for name, label in prompt.HELPER_TASKS]
        registered = [entry["key"] for entry in wording.BUILTIN_PROMPTS]
        self.assertEqual(set(tasks) - set(registered), set(), "helper jobs whose prompt is not in wording.py")
        self.assertEqual(sorted(self.systems(None)), sorted(tasks), "a helper job is missing from this test")
        reworded = self.systems(dict((task, "REWORDED " + task) for task in tasks))
        for task in tasks:
            self.assertTrue(reworded[task].startswith("REWORDED " + task), task + " ignores the preset's wording")

    def test_the_story_models_rules_can_be_reworded_too(self):
        for record, keys in ((True, ("narrator_mechanics", "narrator_records", "leaving_records", "narrator_layout")), (False, ("narrator_mechanics", "narrator_prose", "leaving_prose", "narrator_layout"))):
            preset = dict(self.preset, prompts=dict((key, "REWORDED " + key) for key in keys))
            system = prompt.narrator_prompt(self.card, self.state, preset, "I wait.", [], record=record)[0]
            for key in keys:
                self.assertIn("REWORDED " + key, system)
        story = [entry["key"] for entry in wording.BUILTIN_PROMPTS if entry["reader"] == "story"]
        self.assertEqual(sorted(story), ["card_instructions", "card_reminder", "leaving_prose", "leaving_records", "narrator_check", "narrator_header", "narrator_knowledge", "narrator_layout", "narrator_mechanics", "narrator_prose", "narrator_records"])

    def test_a_thinking_model_can_be_given_a_check_to_run_first(self):
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [])
        self.assertNotIn("[Before you write]", system + messages[-1]["content"])                     # only when the player switches it on
        preset = dict(self.preset, prompts={"narrator_check": "THINK IT THROUGH"})
        system, messages = prompt.narrator_prompt(self.card, self.state, preset, "I wait.", [], check=True)
        last = messages[-1]["content"]
        self.assertTrue(last.index("I wait.") < last.index("THINK IT THROUGH"))                      # after what the player said
        self.assertNotIn("THINK IT THROUGH", system)                                                 # and not in the part that is cached
        for written in ("<check>1. fits. 2. fine.</check>\n\nRain falls.", "<think>\nhm\n</think>Rain falls.", "<check>1. fits.\n2. fine.\n\nRain falls."):
            self.assertEqual(prompt.parse_narration(written)[0], "Rain falls.")
        self.assertEqual(prompt.parse_narration("Rain falls. She says <check this>.")[0], "Rain falls. She says <check this>.")

    def test_the_story_keeps_a_time_and_place_line(self):
        line = "[ 🕰️ 09:40 PM | 🗓️ Day 1 - Tuesday, March 3, 1422 | 📍 Rusty Lantern - By the hearth | 🌧️ Rain, 44 °F ]"
        self.assertEqual(prompt.split_header(line + "\n\nRain drums on the roof."), (line, "Rain drums on the roof."))
        self.assertEqual(prompt.split_header("  " + line + "\nRain."), (line, "Rain."))
        for plain in ("Rain drums on the roof.", "[She laughs.]\n\nRain.", "\"[sic]\" he wrote."):
            self.assertEqual(prompt.split_header(plain), (None, plain))                                # a story that merely opens with a bracket is left alone
        # Seen with a real model: the form written out first, then the real line after a remark.
        drafted = prompt.DEFAULT_HEADER + "\n\nI need to fill this in properly, so:\n\n" + line + "\n\nRain drums on the roof."
        self.assertEqual(prompt.split_header(drafted), (line, "Rain drums on the roof."))
        self.assertEqual(prompt.split_header(prompt.DEFAULT_HEADER + "\n\nRain."), (None, "Rain."))   # the bare form is never shown as a line
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [])
        self.assertNotIn("[Time and place line]", system)                                             # only when it is switched on
        self.state["history"].append({"player": "I sit.", "results": [], "narration": "You sit.", "header": line})
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [], header=True)
        self.assertIn(prompt.DEFAULT_HEADER, system)
        self.assertEqual(messages[3]["content"], line + "\n\nYou sit.")                               # each reply goes back headed by its own line
        self.assertFalse(messages[1]["content"].startswith("["))                                      # the card gives no starting line
        self.card.data["world"].update(header_format="HH:MM | Day # of the Thaw | Place", header_start="06:00 | Day 1 of the Thaw | {{user}}'s camp")
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [], header=True)
        self.assertIn("[ HH:MM | Day # of the Thaw | Place ]", system)
        self.assertTrue(messages[1]["content"].startswith("[ 06:00 | Day 1 of the Thaw | Traveler's camp ]\n\n"))
        self.assertEqual(card.check_card(self.card.data), [])
        # The helpers read the line and never write it: the bookkeeper to judge how long things took, the summary and journal to say when.
        books = prompt.bookkeeper_prompt(self.card, self.state, "I sleep.", [], "Morning.", clock=(line, "[ 🕰️ 07:00 AM | 🗓️ Day 2 ]"))[1][0]["content"]
        self.assertIn("Before this text: 🕰️ 09:40 PM", books)
        self.assertIn("With this text: 🕰️ 07:00 AM | 🗓️ Day 2", books)
        self.assertNotIn("for reference", prompt.bookkeeper_prompt(self.card, self.state, "I sleep.", [], "Morning.")[1][0]["content"])
        self.assertIn("Time and place: 🕰️ 09:40 PM", prompt.summary_prompt(self.card, self.state, self.state["history"])[1][0]["content"])
        self.assertIn("Time and place: 🕰️ 09:40 PM", prompt.journal_prompt(self.card, self.state, self.state["history"])[1][0]["content"])
        self.state["header"] = line
        self.assertIn("Time and place now, as the story has it: 🕰️ 09:40 PM", prompt.world_prompt(self.card, self.state)[1][0]["content"])
        self.card.data["world"]["header_format"] = "a\nb"
        self.assertEqual(len(card.check_card(self.card.data)), 1)

    def test_the_cards_own_instructions_are_given_the_last_word(self):
        self.card.data["world"]["narrator_instructions"] = "Write long, slow scenes."
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [])
        self.assertIn("[This story's own instructions]", system)
        self.assertIn("comes first", system.split("[This story's own instructions]")[1].split("Write long, slow scenes.")[0])
        self.assertTrue(messages[-1]["content"].rstrip().endswith("they win."))                       # after the player's message and the preset's reminders
        preset = dict(self.preset, prompts={"card_instructions": "AUTHOR SAYS: {{instructions}}", "card_reminder": "MIND THE AUTHOR"})
        system, messages = prompt.narrator_prompt(self.card, self.state, preset, "I wait.", [])
        self.assertIn("AUTHOR SAYS: Write long, slow scenes.", system)
        self.assertTrue(messages[-1]["content"].endswith("MIND THE AUTHOR"))
        self.card.data["world"]["narrator_instructions"] = ""                                         # a card without any: nothing is said about them
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [])
        self.assertNotIn("own instructions", system + messages[-1]["content"])

    def test_the_editors_show_each_prompt_the_way_it_is_really_sent(self):
        """Beside each helper prompt the editors list the sections sent under it. That list is
        written by hand in wording.py, so it is checked here against what the builders send."""
        c, s = self.card, self.state
        c.locations["cellar"]["hidden"] = True
        s = __import__("aigame.state", fromlist=["new_game"]).new_game(c)
        s["history"].append({"player": "hi", "results": [], "narration": "Rain."})
        results = [{"ok": True, "message": "Traveler buys stew."}]
        paid = dict(s, paid=[{"quest": "The Missing Courier", "turn": s["turn"], "money": 15, "stats": {}, "items": {}}])
        sent = {
            "resolve_actions": [prompt.resolver_prompt(c, s, "I wait.")],
            "suggest_choices": [prompt.suggest_prompt(c, s, 3)],
            "summarize": [prompt.summary_prompt(c, s, s["history"])],
            "direct_scene": [prompt.director_prompt(c, s, ["Rain."])],
            "record_changes": [prompt.bookkeeper_prompt(c, s, "I wait.", results, "Rain."), prompt.bookkeeper_prompt(c, s, "I wait.", [], "Rain.", quests=False), prompt.bookkeeper_prompt(c, paid, "I wait.", [], "Rain.", clock=("[ 9 PM | Day 1 ]", "[ 7 AM | Day 2 ]"))],
            "judge_quests": [prompt.judge_prompt(c, s, "I wait.", "Rain.")],
            "write_journal": [prompt.journal_prompt(c, s, s["history"])],
            "move_world": [prompt.world_prompt(c, s)],
            "keep_time": [prompt.timekeeper_prompt(c, s, "[ 9:00 PM | Day 1 ]", "[ 8:00 PM | Day 1 ]", ["It went backwards."], "I wait.", "Rain."),
                          prompt.timekeeper_prompt(c, s, None, None, ["No line."], "I wait.", "Rain.")],
            "recall_memory": [prompt.recall_prompt(c, s, "I wait.")],
        }
        self.assertEqual(sorted(sent), sorted(name for name, label in prompt.HELPER_TASKS))
        for task, builds in sent.items():
            listed = [section["heading"] for section in wording.builtin_prompt(task)["sends"]]
            seen = set()
            for system, messages in builds:
                self.assertEqual([m["role"] for m in messages], ["user"], task)        # one message under the instruction, as the editors say
                found = re.findall(r"^\[[^\]\n]+\]$", messages[0]["content"], re.M)
                self.assertEqual(found, [h for h in listed if h in found], task + " sends its sections in another order, or one the editors do not list")
                seen |= set(found)
            self.assertEqual(set(listed) - seen, set(), task + ": the editors list a section that is never sent")
        page = read("creator", "static", "app.js")
        card_pages = set(re.findall(r'^  \{ id: "(\w+)", title:', page, re.M))
        for entry in wording.BUILTIN_PROMPTS:
            self.assertTrue(entry["where"], entry["key"])
            self.assertEqual(set(entry["part_sources"]), set(entry["parts"]), entry["key"])
            for told in entry["sends"] + list(entry["part_sources"].values()):
                self.assertTrue(told["source"], entry["key"])
                kind, _, target = (told["link"] or "none:").partition(":")
                self.assertIn(kind, ("none", "card", "preset"), told["link"])
                if kind != "none":
                    self.assertIn(target.split("/")[0], card_pages, "%s points at a page the creator does not have: %s" % (entry["key"], told["link"]))
                if "/" in target:
                    self.assertIn(target.split("/")[1], [e["key"] for e in wording.BUILTIN_PROMPTS])

    def test_a_part_the_game_fills_in_cannot_be_lost_by_an_edit(self):
        kept = self.systems({"record_changes": "Record changes. {{actions}}"})["record_changes"]
        self.assertIn('"type": "change_stat"', kept)
        self.assertIn("- Costs and harm.", kept)                   # {{checks}} was left out of the edit, so it is added at the end
        self.assertNotIn("{{", kept.replace("{{user}}", ""))
        self.assertEqual(self.systems({"summarize": "   "}), self.systems(None))       # an emptied prompt falls back to the game's own

    def test_preset_files_and_format_agree_with_the_engine(self):
        schema = json.loads(read("spec", "preset.schema.json"))
        self.assertEqual(tuple(schema["properties"]["blocks"]["items"]["properties"]["slot"]["enum"]), wording.SLOTS)
        for entry in wording.BUILTIN_PROMPTS:
            self.assertIn(entry["key"], schema["properties"]["prompts"]["description"])
            for part in entry["parts"]:
                self.assertIn("{{%s}}" % part, entry["text"], entry["key"])
        for name in sorted(os.listdir(os.path.join(ROOT, "presets"))):
            if name.endswith(".preset.json"):
                self.assertEqual(wording.check_preset(json.loads(read("presets", name))), [], name)
        broken = {"spec": "aigame-preset", "name": "", "blocks": [{"id": "a", "kind": "slot", "slot": "weather"}, {"id": "a", "kind": "text"}],
                  "prompts": {"sing": "la"}, "sampling": {"temperature": "hot"}}
        problems = "\n".join(wording.check_preset(broken))
        for expected in ("needs a name", "unknown built-in part 'weather'", "two blocks share the id a", "needs its text", "Recent turns", "unknown entry 'sing'", "sampling.temperature must be a number"):
            self.assertIn(expected, problems)


class ForTheModelOnlyTest(unittest.TestCase):
    def test_a_characters_notes_reach_the_model_and_never_the_screen(self):
        from aigame.card import load_card
        from aigame.state import new_game
        sample = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(sample)
        system = prompt.narrator_prompt(sample, state, json.loads(read("presets", "default.preset.json")), "I wait.", [], record=False)[0]
        self.assertIn("Notes for the narrator: Saw the courier's horse come back without a rider", system)
        self.assertIn("ai_notes", CARD_SCHEMA["properties"]["characters"]["items"]["properties"])
        self.assertIn('k: "ai_notes"', read("creator", "static", "app.js"))
        for name in os.listdir(os.path.join(ROOT, "game")):
            if name.endswith(".rpy"):
                self.assertNotIn("ai_notes", read("game", name), "the game's screens must not read a character's notes")
        for character in sample.data["characters"]:                 # what the People list shows gives nothing away
            self.assertNotIn("told no one", character["description"])
            self.assertNotIn("never reaches", character["description"])


class TogglesTest(unittest.TestCase):
    def test_no_switch_assumes_its_setting_already_exists(self):
        """Ren'Py's ToggleDict raises if the key is missing, and a player's saved settings never have
        the keys that were added after they were saved. Pressing "Use a bookkeeper" crashed that way.
        Switches go through flip(), which takes a default; ToggleDict is left only where the key is
        created together with the dict it lives in."""
        always_there = {'ToggleDict(utility, "enabled")', 'ToggleDict(entry, "connection", "main", slot)'}
        found = set()
        for name in os.listdir(os.path.join(ROOT, "game")):
            if name.endswith(".rpy"):
                found |= set(re.findall(r"Toggle(?:Dict|Field)\((?:[^()]|\([^()]*\))*\)", read("game", name)))
        self.assertEqual(found - always_there, set())
        self.assertIn("def flip(target, key, default, one=True, other=False):", read("game", "llm.rpy"))


class ReleaseTest(unittest.TestCase):
    def test_a_download_carries_the_game_and_creator_and_nothing_personal(self):
        options = read("game", "options.rpy")
        rules = re.findall(r"build\.classify\('([^']+)', ('?\w+'?)\)", options)
        order = [pattern for pattern, where in rules]
        for shipped in ("cards/rusty_lantern/**", "cards/quiet_cafe/**", "presets/default.preset.json", "creator/**", "spec/**"):
            self.assertIn((shipped, "'all'"), rules)
        for kept_out in ("cards/**", "presets/**", "tests/**", "tools/**", "exports/**", "game/saves/**"):
            self.assertIn((kept_out, "None"), rules)
        # The first pattern a file matches decides, so the samples must come before the rule that keeps other cards out.
        self.assertLess(order.index("cards/rusty_lantern/**"), order.index("cards/**"))
        self.assertLess(order.index("presets/default.preset.json"), order.index("presets/**"))
        # The launchers start the executable the build makes, under the name the build gives it.
        name = re.search(r'define build\.name = "(\w+)"', options).group(1)
        self.assertIn("%s.exe" % name, read("SI-Station Creator.bat"))
        self.assertIn("%s.sh" % name, read("si-station-creator"))
        self.assertTrue(os.access(os.path.join(ROOT, "si-station-creator"), os.X_OK))
        for launcher in ("SI-Station Creator.bat", "si-station-creator"):
            self.assertIn("SI_STATION_CREATOR=1", read(launcher))
            self.assertIn("build.classify('%s'" % launcher, options)
        workflow = read(".github", "workflows", "release.yml")
        self.assertIn("--package pc --package mac", workflow)
        self.assertIn("unittest discover tests", workflow)


class ButtonFunctionsTest(unittest.TestCase):
    def test_functions_run_by_buttons_return_nothing(self):
        """In Ren'Py, when a button runs Function(f) and f returns a value, that value closes the
        screen or menu the button is on. Twice that has thrown the player out of a screen. Every
        function a button runs must end without returning anything, unless closing is the point."""
        closing_is_the_point = {
            "go_to",                # travelling ends the input screen so the move is played as a turn
            "import_from_file",     # choosing a card ends the insert-card screen
        }
        sources = dict((name, read("game", name)) for name in os.listdir(os.path.join(ROOT, "game")) if name.endswith(".rpy"))
        everything = "\n".join(sources.values())
        used = set(re.findall(r"Function\(([a-z_]+)[,)]", everything))
        self.assertGreater(len(used), 10)
        offenders = []
        for name in sorted(used - closing_is_the_point):
            found = re.search(r"^(    )def %s\(.*?\):\n((?:(?:        .*)?\n)+)" % name, everything, re.M)
            if not found:
                continue                                    # a Ren'Py built-in such as renpy.full_restart
            body = re.sub(r"^        def .*?(?=^        \S)", "", found.group(2), flags=re.M | re.S)   # ignore helpers nested inside it
            for line in body.splitlines():
                if re.match(r"\s+return\s+\S", line) and not re.match(r"\s+return (None|set_status\()", line):
                    offenders.append("%s: %s" % (name, line.strip()))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
