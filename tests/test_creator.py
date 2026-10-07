"""Run with: python3 -m unittest discover tests"""

import base64
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))
sys.path.insert(0, os.path.join(ROOT, "creator"))

import server  # noqa: E402
import sillytavern  # noqa: E402
from aigame.card import check_card, load_card  # noqa: E402
from aigame import wording  # noqa: E402
from templates import new_card  # noqa: E402


def png_with_text(keyword, text):
    """A 1x1 PNG carrying one tEXt chunk, the way SillyTavern stores a character."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"tEXt", keyword + b"\x00" + text) + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b""))


TAVERN = {"spec": "chara_card_v2", "data": {
    "name": "Captain Vale", "description": "{{char}} commands the airship Kestrel.", "personality": "Stern but fair.",
    "scenario": "{{user}} has stowed away aboard {{char}}'s ship.", "first_mes": "\"Well,\" {{char}} says, \"what have we here?\"",
    "mes_example": "<START>\n{{char}}: Hold fast.", "creator": "someone", "creator_notes": "Best with long replies.", "tags": ["airship", "adventure"],
    "character_book": {"entries": [
        {"keys": ["Kestrel", "ship"], "content": "The Kestrel is a patched-up courier airship.", "priority": 5},
        {"keys": [], "content": "The sky lanes are patrolled.", "constant": True},
        {"keys": ["off"], "content": "ignored", "enabled": False}]}}}


class TemplateAndImportTest(unittest.TestCase):
    def test_every_template_is_a_valid_card(self):
        for template in ("blank", "casual", "rpg", "nonsense"):
            self.assertEqual(check_card(new_card(template, "My First Card!")), [], template)
        self.assertEqual(new_card("rpg", "My First Card!")["meta"]["id"], "my_first_card")

    def test_sillytavern_json_becomes_a_casual_card(self):
        card, avatar = sillytavern.convert(json.dumps(TAVERN).encode())
        self.assertIsNone(avatar)
        self.assertEqual(check_card(card), [])
        self.assertEqual((card["meta"]["id"], card["meta"]["author"], card["meta"]["creator_note"]), ("captain_vale", "someone", "Best with long replies."))
        self.assertEqual(card["display"], {"mode": "text"})
        who = card["characters"][0]
        self.assertEqual(who["description"], "Captain Vale commands the airship Kestrel.")
        self.assertIn("{{user}} has stowed away aboard Captain Vale's ship.", card["world"]["description"])
        self.assertTrue(card["world"]["opening"].startswith('"Well," Captain Vale says'))
        self.assertEqual(card["lorebook"], [
            {"id": "lore_1", "content": "The Kestrel is a patched-up courier airship.", "keys": ["Kestrel", "ship"], "priority": 5},
            {"id": "lore_2", "content": "The sky lanes are patrolled.", "always_on": True}])
        self.assertTrue(card["rules"]["features"]["relationships"])

    def test_sillytavern_png_keeps_the_picture(self):
        blob = png_with_text(b"chara", base64.b64encode(json.dumps(TAVERN["data"]).encode()))      # V1 is flat
        card, avatar = sillytavern.convert(blob)
        self.assertEqual(avatar, blob)
        self.assertEqual(check_card(card), [])
        self.assertEqual(card["characters"][0]["sprites"], {"neutral": "assets/sprites/captain_vale/neutral.png"})
        self.assertNotIn("display", card)

    def test_lorebook_from_a_world_info_file(self):
        world_info = {"entries": {
            "10": {"uid": 10, "key": ["Clover Kingdom", "kingdom"], "comment": "The Clover Kingdom", "content": "A kingdom ruled by the Wizard King.", "order": 100},
            "2": {"uid": 2, "key": "grimoire, book", "comment": "Grimoires", "content": "{{char}} received a grimoire at fifteen.", "constant": False, "order": 50},
            "3": {"uid": 3, "key": [], "comment": "", "content": "Magic is everything in this world.", "constant": True},
            "4": {"uid": 4, "key": ["skip"], "comment": "Off", "content": "switched off", "disable": True},
            "5": {"uid": 5, "key": ["empty"], "comment": "Empty", "content": "  "}}}
        found = sillytavern.convert_lorebook(json.dumps(world_info).encode())
        self.assertEqual([e["title"] for e in found], ["Grimoires", "", "The Clover Kingdom"])            # in number order, skips left out
        self.assertEqual(found[0], {"title": "Grimoires", "content": "{{char}} received a grimoire at fifteen.", "keys": ["grimoire", "book"], "priority": 50})
        self.assertEqual(found[1], {"title": "", "content": "Magic is everything in this world.", "always_on": True})

        card = new_card("casual", "Black Clover")
        card["characters"] = [{"id": "asta", "name": "Asta", "description": "Loud."}]
        card["lorebook"] = [{"id": "grimoires", "content": "already here"}]
        self.assertEqual(sillytavern.add_lore(card, found), 3)
        self.assertEqual([e["id"] for e in card["lorebook"]], ["grimoires", "grimoires_2", "lore_1", "the_clover_kingdom"])
        self.assertEqual(card["lorebook"][1]["content"], "Asta received a grimoire at fifteen.")             # one character, so {{char}} is them
        self.assertEqual(check_card(card), [])

    def test_lorebook_from_a_character_card(self):
        for blob in (json.dumps(TAVERN).encode(), json.dumps(TAVERN["data"]["character_book"]).encode(),
                     png_with_text(b"chara", base64.b64encode(json.dumps(TAVERN).encode()))):
            found = sillytavern.convert_lorebook(blob)
            self.assertEqual([(e["content"][:11], e.get("keys"), e.get("always_on"), e.get("priority")) for e in found],
                             [("The Kestrel", ["Kestrel", "ship"], None, 5), ("The sky lan", None, True, None)])
        for blob in (b'{"name": "No book here"}', b'{"entries": {"1": {"content": "", "key": ["x"]}}}', b"nope"):
            self.assertRaises(sillytavern.ImportError_, sillytavern.convert_lorebook, blob)

    def test_files_that_are_not_cards_say_so(self):
        for blob in (png_with_text(b"comment", b"hello"), b"just some text", b"[1, 2]", png_with_text(b"chara", b"!!!not base64!!!")):
            self.assertRaises(sillytavern.ImportError_, sillytavern.convert, blob)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        cards = os.path.join(self.work, "cards")
        shutil.copytree(os.path.join(ROOT, "cards", "rusty_lantern"), os.path.join(cards, "rusty_lantern"))
        os.makedirs(os.path.join(self.work, "presets"))
        server.Handler.workspace = server.Workspace(cards, os.path.join(self.work, "exports"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.work)

    def call(self, method, path, body=None, raw=False, headers=None):
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request) as reply:
                payload = reply.read()
                return reply.status, (json.loads(payload) if reply.headers["Content-Type"] == "application/json" else payload)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_edit_save_and_check(self):
        status, listing = self.call("GET", "/api/projects")
        self.assertEqual([(p["id"], p["problems"]) for p in listing["projects"]], [("rusty_lantern", 0)])
        status, reply = self.call("GET", "/api/projects/rusty_lantern")
        card = reply["card"]
        card["characters"][0]["location"] = "the_moon"
        status, reply = self.call("PUT", "/api/projects/rusty_lantern", card)
        self.assertEqual(reply["problems"], ["character mira is in unknown location 'the_moon'"])
        self.assertEqual(self.call("POST", "/api/projects/rusty_lantern/pack")[0], 409)          # will not pack a broken card
        card["characters"][0]["location"] = "common_room"
        self.assertEqual(self.call("PUT", "/api/projects/rusty_lantern", card)[1]["problems"], [])

    def test_create_upload_pack_and_trash(self):
        status, made = self.call("POST", "/api/projects", {"title": "Sky Pirates", "template": "rpg"})
        self.assertEqual(made["id"], "sky_pirates")
        self.assertEqual(self.call("POST", "/api/projects", {"title": "Sky Pirates"})[1]["id"], "sky_pirates_2")   # never overwrites
        picture = png_with_text(b"comment", b"x")
        self.assertEqual(self.call("POST", "/api/projects/sky_pirates/asset?path=assets/cover.png", picture, raw=True)[0], 200)
        self.assertEqual(self.call("GET", "/api/projects/sky_pirates/file/assets/cover.png")[1], picture)
        status, packed = self.call("POST", "/api/projects/sky_pirates/pack")
        self.assertEqual(packed["file"], "sky_pirates.sicard")
        status, blob = self.call("GET", "/api/exports/sky_pirates.sicard")
        target = os.path.join(self.work, "downloaded.sicard")
        with open(target, "wb") as f:
            f.write(blob)
        self.assertEqual(load_card(target).read_asset("assets/cover.png"), picture)                # the game can open what the creator packs
        self.assertEqual(self.call("DELETE", "/api/projects/sky_pirates")[0], 200)
        self.assertEqual(self.call("GET", "/api/projects/sky_pirates")[0], 404)
        self.assertEqual(len(os.listdir(os.path.join(self.work, "cards", ".trash"))), 1)           # moved, not deleted

    def test_presets_are_made_edited_and_trashed(self):
        shutil.copy(os.path.join(ROOT, "presets", "default.preset.json"), os.path.join(self.work, "presets", "default.preset.json"))
        status, listing = self.call("GET", "/api/presets")
        self.assertEqual([(p["id"], p["builtin"], p["problems"]) for p in listing["presets"]], [("default", True, 0)])
        self.assertEqual(len(listing["wording"]), 19)                             # every prompt the game writes itself, for the builder to show
        self.assertEqual(sorted(listing["slots"]), sorted(wording.SLOTS))
        self.assertEqual(self.call("PUT", "/api/presets/default", {"name": "Mine"})[0], 403)      # the game's own is never changed

        status, made = self.call("POST", "/api/presets", {"name": "For small models"})
        self.assertEqual((status, made["id"]), (200, "for_small_models"))
        status, opened = self.call("GET", "/api/presets/for_small_models")
        self.assertEqual((opened["preset"]["name"], opened["problems"], opened["builtin"]), ("For small models", [], False))
        opened["preset"]["prompts"] = {"judge_quests": "Be strict. Reply with JSON."}
        opened["preset"]["blocks"][1]["enabled"] = False
        self.assertEqual(self.call("PUT", "/api/presets/for_small_models", opened["preset"]), (200, {"problems": []}))
        with open(os.path.join(self.work, "presets", "for_small_models.preset.json")) as f:
            self.assertEqual(json.load(f)["prompts"], {"judge_quests": "Be strict. Reply with JSON."})
        status, copy_ = self.call("POST", "/api/presets", {"name": "For small models", "copy_of": "for_small_models"})
        self.assertEqual(copy_["id"], "for_small_models_2")
        self.assertEqual(self.call("GET", "/api/presets/for_small_models_2")[1]["preset"]["prompts"], {"judge_quests": "Be strict. Reply with JSON."})
        status, bad = self.call("PUT", "/api/presets/for_small_models_2", {"spec": "aigame-preset", "name": "x", "blocks": []})
        self.assertIn("at least one block", bad["problems"][0])

        # What a prompt looks like when it goes out, with edits that have not been saved yet.
        draft = dict(opened["preset"], prompts={"judge_quests": "Be VERY strict. Reply with JSON."})
        status, shown = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "key": "judge_quests"})
        self.assertEqual((status, shown["system"]), (200, "Be VERY strict. Reply with JSON."))
        self.assertEqual([m["role"] for m in shown["messages"]], ["user"])
        status, books = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "key": "record_changes"})
        self.assertEqual(sorted(books["parts"]), ["actions", "checks", "states"])            # what the game puts in place of each {{part}}
        self.assertTrue(books["parts"]["actions"].startswith('{"type": "add_item"'))
        self.assertTrue(books["parts"]["checks"].startswith("- Costs and harm."))
        self.assertIn(books["parts"]["states"], books["system"])
        self.assertIn("[Quests in progress]\n- The Missing Courier", shown["messages"][0]["content"])
        status, shown = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "key": "narrator_prose"})
        self.assertIn("A bookkeeper reads what you write", shown["system"])
        self.assertIn("[The scene right now]", shown["messages"][-1]["content"])
        # One whole turn, as the request body that goes to the story model.
        draft["blocks"][0]["content"] = "You are the narrator. UNSAVED EDIT."
        status, turn = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "turn": True, "provider": "nanogpt"})
        self.assertEqual((status, turn["url"]), (200, "https://nano-gpt.com/api/v1/chat/completions"))
        sent = turn["request"]
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user", "assistant", "user", "assistant", "user", "assistant", "user"])
        self.assertTrue(sent["messages"][0]["content"].startswith("You are the narrator. UNSAVED EDIT."))
        self.assertIn("A bookkeeper reads what you write", sent["messages"][0]["content"])       # the bookkeeper is on unless said otherwise
        self.assertIn("[The scene right now]", sent["messages"][-1]["content"])
        self.assertIn("[Time and place line]", sent["messages"][0]["content"])                      # on unless said otherwise, as in the game
        plain = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "turn": True, "provider": "openrouter", "header": False})[1]
        self.assertNotIn("[Time and place line]", plain["request"]["messages"][0]["content"])
        self.assertNotIn("[Before you write]", sent["messages"][-1]["content"])                    # the check is shown only when ticked
        checked = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "turn": True, "provider": "openrouter", "check": True})[1]
        self.assertIn("[Before you write]", checked["request"]["messages"][-1]["content"])
        self.assertEqual((sent["temperature"], sent["max_tokens"]), (0.9, 2000))
        claude = self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "turn": True, "provider": "anthropic", "bookkeeper": False})[1]
        self.assertEqual(claude["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(claude["request"]["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})   # where the cached part ends
        self.assertIn("<actions>", claude["request"]["system"][0]["text"])
        self.assertNotIn("your key", json.dumps(turn) + json.dumps(claude))
        self.assertEqual(self.call("POST", "/api/preview", {"preset": draft, "card": "rusty_lantern", "turn": True, "provider": "carrier pigeon"})[0], 400)
        for body in ({"preset": draft, "card": "rusty_lantern", "key": "sing"}, {"preset": draft, "card": "nope", "key": "summarize"}):
            self.assertEqual(self.call("POST", "/api/preview", body)[0], 404)
        self.assertEqual(self.call("POST", "/api/preview", {"preset": {"blocks": []}, "card": "rusty_lantern", "key": "summarize"})[0], 409)

        # Export: the preset as a file, which the game takes in with Import a preset file.
        request = urllib.request.Request(self.base + "/api/presets/for_small_models/download")
        with urllib.request.urlopen(request) as reply:
            self.assertEqual(reply.headers["Content-Disposition"], 'attachment; filename="for_small_models.preset.json"')
            exported = os.path.join(self.work, "for_small_models.preset.json")
            with open(exported, "wb") as f:
                f.write(reply.read())
        game_presets = os.path.join(self.work, "game_presets")
        taken = wording.import_preset(exported, game_presets)
        self.assertEqual(taken, "for_small_models")
        with open(os.path.join(game_presets, "for_small_models.preset.json")) as f:
            self.assertEqual(json.load(f)["prompts"], {"judge_quests": "Be strict. Reply with JSON."})
        self.assertEqual(wording.import_preset(exported, game_presets), "for_small_models_2")         # a second time does not overwrite the first
        shutil.copy(exported, os.path.join(self.work, "default.preset.json"))
        self.assertEqual(wording.import_preset(os.path.join(self.work, "default.preset.json"), game_presets), "imported_default")   # never the game's own
        for name, content, why in (("notes.json", b"hello", "not valid JSON"), ("card.json", b'{"spec": "aigame-card"}', "not a usable preset"), ("big.json", b" " * (2 * 1024 * 1024 + 5), "too large")):
            with open(os.path.join(self.work, name), "wb") as f:
                f.write(content)
            with self.assertRaises(wording.PresetError) as refused:
                wording.import_preset(os.path.join(self.work, name), game_presets)
            self.assertIn(why, str(refused.exception))
        self.assertRaises(wording.PresetError, wording.import_preset, os.path.join(self.work, "missing.json"), game_presets)
        self.assertEqual(sorted(os.listdir(game_presets)), ["for_small_models.preset.json", "for_small_models_2.preset.json", "imported_default.preset.json"])
        self.assertEqual(self.call("GET", "/api/presets/nope/download")[0], 404)

        # Import in the creator: the exported file comes back in as a preset of its own, and a bad file is refused with the reason.
        with open(exported, "rb") as f:
            status, back = self.call("POST", "/api/presets/import?name=for_small_models.preset.json", f.read(), raw=True)
        self.assertEqual((status, back["id"]), (200, "for_small_models_3"))                     # _2 is the copy made above; nothing is overwritten
        self.assertEqual(self.call("GET", "/api/presets/for_small_models_3")[1]["preset"]["prompts"], {"judge_quests": "Be strict. Reply with JSON."})
        status, refused = self.call("POST", "/api/presets/import?name=notes.json", b"hello", raw=True)
        self.assertEqual((status, refused["error"]), (400, "That file is not a preset: it is not valid JSON."))
        status, refused = self.call("POST", "/api/presets/import?name=../../x.json", json.dumps({"spec": "aigame-card"}).encode(), raw=True)
        self.assertEqual(status, 400)
        with open(exported, "rb") as f:
            status, odd = self.call("POST", "/api/presets/import?name=..%2F..%2Fevil%20name.JSON", f.read(), raw=True)
        self.assertEqual((status, odd["id"]), (200, "evil_name"))                                # the name is only ever a plain file name in the presets folder
        self.assertEqual(self.call("DELETE", "/api/presets/for_small_models_3")[0], 200)
        self.assertEqual(self.call("DELETE", "/api/presets/evil_name")[0], 200)

        self.assertEqual(self.call("DELETE", "/api/presets/default")[0], 403)
        self.assertEqual(self.call("DELETE", "/api/presets/for_small_models_2")[0], 200)
        self.assertEqual([p["id"] for p in self.call("GET", "/api/presets")[1]["presets"]], ["default", "for_small_models"])
        for path in ("/api/presets/nope", "/api/presets/..%2Fx"):
            self.assertEqual(self.call("GET", path)[0], 404)

    def test_import_and_static_files(self):
        blob = png_with_text(b"chara", base64.b64encode(json.dumps(TAVERN).encode()))
        status, made = self.call("POST", "/api/import/sillytavern", blob, raw=True)
        self.assertEqual((status, made["id"]), (200, "captain_vale"))
        self.assertEqual(self.call("GET", "/api/projects/captain_vale/file/assets/sprites/captain_vale/neutral.png")[1], blob)
        self.assertEqual(self.call("GET", "/api/projects/captain_vale")[1]["problems"], [])
        self.assertEqual(self.call("POST", "/api/import/sillytavern", b"nope", raw=True)[0], 400)
        status, converted = self.call("POST", "/api/convert/lorebook", json.dumps(TAVERN).encode(), raw=True)
        self.assertEqual((status, len(converted["entries"])), (200, 2))
        self.assertEqual(self.call("POST", "/api/convert/lorebook", b'{"name": "x"}', raw=True)[0], 400)
        self.assertIn(b"SI-Station Card Creator", self.call("GET", "/")[1])
        self.assertIn(b"renderFields", self.call("GET", "/app.js")[1])

    def test_requests_that_reach_outside_are_refused(self):
        picture = png_with_text(b"comment", b"x")
        for path in ("../evil.png", "assets/../../evil.png", "card.json", "assets/run.sh", "/etc/passwd.png"):
            self.assertEqual(self.call("POST", "/api/projects/rusty_lantern/asset?path=" + urllib.request.quote(path, safe=""), picture, raw=True)[0], 400, path)
        self.assertEqual(self.call("GET", "/api/projects/..%2F..%2Fgame")[0], 404)
        self.assertEqual(self.call("GET", "/..%2Fserver.py")[0], 404)
        self.assertEqual(self.call("GET", "/api/exports/..%2F..%2Fcards%2Frusty_lantern%2Fcard.json")[0], 404)
        self.assertEqual(self.call("GET", "/api/projects", headers={"Host": "evil.example"})[0], 403)   # a web page elsewhere cannot drive it


if __name__ == "__main__":
    unittest.main()
