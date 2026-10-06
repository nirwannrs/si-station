"""Run with: python3 -m unittest discover tests"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))
sys.path.insert(0, os.path.join(ROOT, "creator"))

import mcp_server  # noqa: E402
from aigame.card import load_card  # noqa: E402

CARD = os.path.join(ROOT, "cards", "rusty_lantern")


class EditTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(CARD, "card.json")) as f:
            self.card = json.load(f)

    def edit(self, op, path, *value):
        mcp_server.apply_edit(self.card, dict({"op": op, "path": path}, **({"value": value[0]} if value else {})))

    def test_reading_by_path(self):
        self.assertEqual(mcp_server.read_path(self.card, "meta.title"), "The Rusty Lantern")
        self.assertEqual(mcp_server.read_path(self.card, "characters[mira].name"), "Mira Oakhand")
        self.assertEqual(mcp_server.read_path(self.card, "quests[missing_courier].stages[0].id"), "ask_around")
        self.assertEqual(mcp_server.read_path(self.card, "locations[-1].id"), "cellar")

    def test_set_add_remove(self):
        self.edit("set", "characters[mira].personality", "Warm.")
        self.edit("set", "rules.features.skills", False)
        self.edit("set", "display.mode", "text")                                  # creates the missing object on the way
        self.edit("add", "characters", {"id": "ghost", "name": "Ghost", "description": "Boo."})
        self.edit("add", "characters[ghost].start.skills", {"skill": "quick_strike"})   # creates the missing list
        self.edit("set", "items[stew]", {"id": "stew", "name": "Thick Stew", "type": "consumable"})
        self.edit("remove", "characters[tobin]")
        self.edit("remove", "meta.creator_note")
        self.assertEqual(self.card["characters"][0]["personality"], "Warm.")
        self.assertEqual(self.card["display"], {"mode": "text"})
        self.assertEqual([c["id"] for c in self.card["characters"]], ["mira", "marsh_bandit", "ghost"])
        self.assertEqual(self.card["characters"][-1]["start"], {"skills": [{"skill": "quick_strike"}]})
        self.assertEqual(mcp_server.read_path(self.card, "items[stew].name"), "Thick Stew")
        self.assertNotIn("creator_note", self.card["meta"])

    def test_mistakes_explain_themselves(self):
        cases = [("set", "characters[nobody].name", "x", "It has: mira, tobin, marsh_bandit"),
                 ("set", "characters[9].name", "x", "has 3 entries"),
                 ("add", "characters", {"id": "mira", "name": "Twin"}, "already has an entry with id 'mira'"),
                 ("add", "meta", "x", "is not a list"),
                 ("add", "characters[mira]", {}, "must end in the list's name"),
                 ("remove", "meta.nothing", None, "has no 'nothing' to remove"),
                 ("set", "meta..title", "x", "Could not read the path"),
                 ("set", "", "x", "A path is needed"),
                 ("set", "meta.title[0]", "x", "is not a list")]
        for op, path, value, expected in cases:
            before = copy.deepcopy(self.card)
            with self.assertRaises(mcp_server.ToolError) as caught:
                self.edit(op, path, *([] if op == "remove" else [value]))
            self.assertIn(expected, str(caught.exception), (op, path))
            self.assertEqual(self.card, before, (op, path))
        self.assertRaises(mcp_server.ToolError, mcp_server.apply_edit, self.card, {"op": "set", "path": "meta.title"})
        self.assertRaises(mcp_server.ToolError, mcp_server.apply_edit, self.card, {"op": "rename", "path": "meta.title", "value": 1})


class SessionTest(unittest.TestCase):
    """Talks to the real server process the way an MCP client does."""

    def setUp(self):
        self.work = tempfile.mkdtemp()
        shutil.copytree(CARD, os.path.join(self.work, "cards", "rusty_lantern"))
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "creator", "mcp_server.py"), "--cards", os.path.join(self.work, "cards"),
                                      "--exports", os.path.join(self.work, "exports")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.next_id = 0

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        shutil.rmtree(self.work)

    def send(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if not notify:
            reply = json.loads(self.proc.stdout.readline())
            self.assertEqual(reply["id"], self.next_id)
            return reply

    def call(self, tool, **args):
        result = self.send("tools/call", {"name": tool, "arguments": args})["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_it_restarts_itself_when_its_code_changes(self):
        """A server that keeps running old code calls good cards broken. It must pick up new code
        without the client reconnecting or losing a request."""
        self.tearDown()
        self.work = tempfile.mkdtemp()
        shutil.copytree(CARD, os.path.join(self.work, "cards", "rusty_lantern"))
        watched = os.path.join(self.work, "stand_in_for_the_code.py")
        with open(watched, "w") as f:
            f.write("x = 1\n")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "creator", "mcp_server.py"), "--cards", os.path.join(self.work, "cards"),
                                      "--exports", os.path.join(self.work, "exports"), "--watch", watched], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        hello = lambda: self.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})["result"]["serverInfo"]["code"]
        first = hello()
        self.assertEqual(hello(), first)                                    # nothing changed: same process, same code
        self.assertFalse(self.call("edit_card", card="rusty_lantern", edits=[{"op": "set", "path": "meta.title", "value": "Before"}])[0])

        with open(watched, "w") as f:
            f.write("this is not python (\n")                              # a half-saved file must not take the server down
        os.utime(watched, ns=(10**18, 10**18))
        self.assertEqual(hello(), first)

        with open(watched, "w") as f:
            f.write("x = 2\n")
        os.utime(watched, ns=(2 * 10**18, 2 * 10**18))
        # Two requests written back to back: the first triggers the restart, and neither may be lost.
        for n, method in ((101, "tools/list"), (102, "ping")):
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method}) + "\n")
        self.proc.stdin.flush()
        replies = [json.loads(self.proc.stdout.readline()) for _ in range(2)]
        self.assertEqual([r["id"] for r in replies], [101, 102])
        self.assertEqual(len(replies[0]["result"]["tools"]), 15)
        self.assertNotEqual(hello(), first)                                 # now running the new code
        self.assertEqual(self.call("get_card", card="rusty_lantern", path="meta.title")[1]["value"], "Before")   # and the work is still there
        self.assertIsNone(self.proc.poll())

    def test_a_whole_session(self):
        hello = self.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual((hello["protocolVersion"], hello["serverInfo"]["name"]), ("2025-03-26", "si-station-cards"))
        self.assertIn("card_format", hello["instructions"])
        self.send("notifications/initialized", notify=True)
        self.assertEqual(self.send("ping")["result"], {})
        tools = self.send("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["card_format", "list_cards", "get_card", "create_card", "edit_card", "replace_card", "add_image", "import_lorebook", "pack_card", "trash_card",
                                                    "preset_format", "list_presets", "get_preset", "save_preset", "trash_preset"])
        self.assertTrue(all(t["description"] and t["inputSchema"]["type"] == "object" for t in tools))

        failed, fmt = self.call("card_format")
        self.assertEqual(fmt["schema"]["title"], "AIGame Card")
        failed, listing = self.call("list_cards")
        self.assertEqual([c["id"] for c in listing["cards"]], ["rusty_lantern"])
        failed, part = self.call("get_card", card="rusty_lantern", path="characters[mira].personality")
        self.assertIn("Blunt", part["value"])

        # an edit that breaks a reference is saved, and says what is now wrong
        failed, result = self.call("edit_card", card="rusty_lantern", edits=[{"op": "set", "path": "characters[mira].location", "value": "moon"}])
        self.assertEqual((failed, result["playable"], result["problems"]), (False, False, ["character mira is in unknown location 'moon'"]))
        failed, packed = self.call("pack_card", card="rusty_lantern")
        self.assertTrue(failed)
        # a batch with one bad edit changes nothing
        failed, result = self.call("edit_card", card="rusty_lantern", edits=[{"op": "set", "path": "meta.title", "value": "Changed"}, {"op": "remove", "path": "meta.nothing"}])
        self.assertTrue(failed)
        self.assertIn("Edit 2 of 2 failed, so nothing was changed", result["error"])
        self.assertEqual(self.call("get_card", card="rusty_lantern", path="meta.title")[1]["value"], "The Rusty Lantern")
        failed, result = self.call("edit_card", card="rusty_lantern", edits=[
            {"op": "set", "path": "characters[mira].location", "value": "common_room"},
            {"op": "add", "path": "locations", "value": {"id": "attic", "name": "Attic", "connections": ["common_room"]}}])
        self.assertEqual((failed, result["playable"], result["applied"]), (False, True, 2))

        # a new card with a picture, packed into something the game can open
        failed, made = self.call("create_card", title="Moon Base", template="casual")
        self.assertEqual((made["card"], made["playable"]), ("moon_base", True))
        failed, image = self.call("add_image", card="moon_base", file=os.path.join(CARD, "assets", "cover.jpg"), save_as="assets/cover.jpg")
        self.assertFalse(failed, image)
        self.call("edit_card", card="moon_base", edits=[{"op": "set", "path": "meta.cover", "value": "assets/cover.jpg"}])
        failed, packed = self.call("pack_card", card="moon_base")
        self.assertFalse(failed, packed)
        self.assertEqual(load_card(packed["file"]).data["meta"]["cover"], "assets/cover.jpg")

        book = os.path.join(self.work, "world.json")
        with open(book, "w") as f:
            json.dump({"entries": {"0": {"key": ["moon"], "comment": "The Moon", "content": "It is made of cheese."}}}, f)
        failed, imported = self.call("import_lorebook", card="moon_base", file=book)
        self.assertEqual((failed, imported["added"], imported["playable"]), (False, 1, True))
        self.assertEqual(self.call("get_card", card="moon_base", path="lorebook[the_moon].keys")[1]["value"], ["moon"])
        self.assertTrue(self.call("import_lorebook", card="moon_base", file=os.path.join(CARD, "card.json"))[0])   # not a lorebook

        for tool, args in [("get_card", {"card": "nope"}), ("get_card", {"card": "../../game"}), ("add_image", {"card": "moon_base", "file": "/etc/hosts", "save_as": "assets/x.png"}),
                           ("add_image", {"card": "moon_base", "file": os.path.join(CARD, "assets", "cover.jpg"), "save_as": "../cover.jpg"}), ("edit_card", {"card": "moon_base", "edits": []})]:
            self.assertTrue(self.call(tool, **args)[0], (tool, args))
        failed, gone = self.call("trash_card", card="moon_base")
        self.assertEqual(os.listdir(gone["moved_to"])[0].split("-")[0], "moon_base")

        # Presets: read the format, make one with a reworded helper prompt, and be told what is wrong with a bad one.
        failed, format_ = self.call("preset_format")
        self.assertEqual(sorted(e["key"] for e in format_["builtin_prompts"] if e["reader"] == "helper"),
                         ["direct_scene", "judge_quests", "record_changes", "resolve_actions", "suggest_choices", "summarize", "write_journal"])
        self.assertIn("prompts", format_["schema"]["properties"])
        self.assertEqual(self.call("list_presets")[1]["presets"], [])
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            mine = dict(json.load(f), name="Terse", prompts={"summarize": "Summarize in fifty words."})
        failed, saved = self.call("save_preset", preset="terse", content=mine)
        self.assertEqual((failed, saved["usable"], saved["problems"]), (False, True, []))
        self.assertEqual(self.call("get_preset", preset="terse")[1]["content"]["prompts"], {"summarize": "Summarize in fifty words."})
        self.assertEqual([(p["id"], p["name"], p["problems"]) for p in self.call("list_presets")[1]["presets"]], [("terse", "Terse", 0)])
        failed, saved = self.call("save_preset", preset="terse", content=dict(mine, prompts={"sing": "la"}))
        self.assertEqual((failed, saved["usable"]), (False, False))
        self.assertIn("unknown entry 'sing'", saved["problems"][0])
        for tool, args in [("save_preset", {"preset": "default", "content": mine}), ("save_preset", {"preset": "../x", "content": mine}), ("get_preset", {"preset": "nope"}),
                           ("trash_preset", {"preset": "default"})]:
            self.assertTrue(self.call(tool, **args)[0], (tool, args))
        self.assertFalse(self.call("trash_preset", preset="terse")[0])
        self.assertEqual(self.call("list_presets")[1]["presets"], [])
        self.assertIn("error", self.send("tools/call", {"name": "format_disk", "arguments": {}}))
        self.assertEqual(self.send("resources/list")["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
