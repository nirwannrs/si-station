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
from aigame import actions, card, prompt  # noqa: E402
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
