"""SI-Station card creator: a local web app.

    python3 creator/server.py            then open http://127.0.0.1:8770

It edits card folders in the game's cards/ directory, so a card being made can be inserted in the
game as it is, and packs finished cards into .sicard files under exports/. The checks and the
packing are the game's own code (game/aigame/card.py), so the creator and the engine cannot drift.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "game"))
sys.path.insert(0, HERE)

from aigame.card import BUILTIN_STATES, CAPABILITIES, EXTENSION, CardError, check_card, load_card, pack_card  # noqa: E402
from aigame import llm as game_llm  # noqa: E402
from aigame import prompt as game_prompt  # noqa: E402
from aigame.state import new_game  # noqa: E402
from aigame.wording import BUILTIN_PROMPTS, PresetError, builtin_prompt, check_preset, take_preset  # noqa: E402
import freshness  # noqa: E402
import sillytavern  # noqa: E402
from templates import RULES, new_card, slug  # noqa: E402

STATIC = os.path.join(HERE, "static")
ID = re.compile(r"^[a-z0-9_]+$")
ASSET = re.compile(r"^assets/[A-Za-z0-9_./-]+$")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
MAX_UPLOAD = 50 * 1024 * 1024
PRESET_ENDING = ".preset.json"
GAME_PRESET = os.path.join(ROOT, "presets", "default" + PRESET_ENDING)

# What each built-in part of a preset is, for the builder. The names are the engine's (wording.SLOTS).
SLOT_NOTES = {
    "world": "The card's world and opening situation.",
    "narrator_instructions": "The card creator's own guidance to the narrator.",
    "persona": "Who the player is playing.",
    "characters": "The card's cast.",
    "action_protocol": "How the game's rules work. Always sent. Its wording can be changed on the next page, The game's rules.",
    "history": "The story so far. Everything above it is sent once and cached; everything below is sent fresh each turn.",
    "summary": "The summary of older turns, once there is one.",
    "lorebook": "Background facts that have just become relevant.",
    "state": "The scene and the game's current state: who is present, health, items, places.",
    "quests": "Open quests and their objectives.",
}


def preview_prompt(card, preset, key):
    """Exactly what the game would send for one of its own prompts, with this preset, a few turns
    into a game of this card: {"system": text, "messages": [{"role", "content"}], "parts": {...}}.
    The story and the player's lines are stand-ins; everything else is built by the game's own code."""
    state = new_game(card)
    state["history"].append({"player": "I step inside.", "results": [], "narration": "(An earlier reply by the story model.)"})
    built = _preview_build(card, state, preset, key)
    if built is None:
        raise Refused(404, "No such prompt.")
    return {"system": built[0], "messages": [{"role": m["role"], "content": m["content"]} for m in built[1]], "parts": preview_parts(card, state, preset, key)}


# The story model a turn can be previewed for: provider name -> a model id that shows that provider's form of the request.
PREVIEW_MODELS = {"openrouter": "anthropic/claude-opus-5.5", "nanogpt": "z-ai/glm-5.3", "anthropic": "claude-opus-5-5", "custom": "your-model", "local": "your-model"}


def preview_turn(card, preset, provider, bookkeeper, check=False, header=True):
    """The request the game would send to the story model for one turn, a few turns into a game of
    this card: {"url", "request"}. It is made by the game's own code (the prompt builder, then the
    provider adapter), so it is the real body: the same fields, the same order, the same cache marks.
    The model name, the story and the player's lines are stand-ins, and no key is involved."""
    if provider not in PREVIEW_MODELS:
        raise Refused(400, "Unknown provider.")
    state = new_game(card)
    for said, reply in (("I step inside.", "(An earlier reply by the story model.)"), ("I look around.", "(The reply before this one.)")):
        state["history"].append({"player": said, "results": [], "narration": reply})
    results = [{"ok": True, "message": "(Something the game applied for the player this turn.)"}]
    system, messages = game_prompt.narrator_prompt(card, state, preset, "I ask what is going on.", results, record=not bookkeeper, check=check, header=header)
    connection = {"provider": provider, "base_url": "https://your-endpoint.example/v1" if provider == "custom" else "", "api_key": "(your key)"}
    sent = game_llm.chat_request(connection, PREVIEW_MODELS[provider], system, messages, preset.get("sampling") or {})
    return {"url": sent["url"], "request": sent["json"]}


def _preview_build(card, state, preset, key):
    said = "I look around and ask what is going on."
    reply = "(The story model's reply would be here: a few paragraphs of story.)"
    results = [{"ok": True, "message": "(Something the game applied for the player this turn.)"}]
    prompts = preset.get("prompts") if isinstance(preset.get("prompts"), dict) else {}
    if key in ("narrator_mechanics", "narrator_prose", "leaving_prose", "narrator_records", "leaving_records", "card_instructions", "card_reminder", "narrator_layout", "narrator_check", "narrator_knowledge", "narrator_header"):
        return game_prompt.narrator_prompt(card, state, preset, said, results, record=key.endswith("records"), check=True, header=True)
    return {
        "resolve_actions": lambda: game_prompt.resolver_prompt(card, state, said, prompts=prompts),
        "record_changes": lambda: game_prompt.bookkeeper_prompt(card, state, said, results, reply, quests=not card.quests, prompts=prompts),
        "judge_quests": lambda: game_prompt.judge_prompt(card, state, said, reply, prompts=prompts),
        "direct_scene": lambda: game_prompt.director_prompt(card, state, game_prompt.split_paragraphs(reply + "\n\n(Its second paragraph.)"), prompts=prompts),
        "suggest_choices": lambda: game_prompt.suggest_prompt(card, state, (preset.get("suggestions") or {}).get("count", 3), prompts=prompts),
        "summarize": lambda: game_prompt.summary_prompt(card, state, state["history"], prompts=prompts),
        "write_journal": lambda: game_prompt.journal_prompt(card, state, state["history"], prompts=prompts),
        "move_world": lambda: game_prompt.world_prompt(card, state, prompts=prompts),
        "keep_time": lambda: game_prompt.timekeeper_prompt(card, state, "(The line that headed the reply before.)", "(The line that heads this reply.)",
                                                           ["(What the game found wrong with the line.)"], said, reply, prompts=prompts),
    }.get(key, lambda: None)()


def preview_parts(card, state, preset, key):
    """What the game puts in place of each {{part}} of one prompt, for this card: {name: text}.
    Found by building the prompt once more with a wording that is nothing but the parts, fenced."""
    names = list(builtin_prompt(key)["parts"])
    if not names:
        return {}
    fenced = dict(preset, prompts=dict(preset.get("prompts") or {}, **{key: "".join("\x01%s\x02{{%s}}\x03" % (n, n) for n in names)}))
    system = _preview_build(card, state, fenced, key)[0]
    return dict((name, text.strip("\n")) for name, text in re.findall(r"\x01(\w+)\x02(.*?)\x03", system, re.S))


class Refused(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


class Workspace(object):
    """The folder of card folders being edited, and where packed cards go."""

    def __init__(self, cards, exports, presets=None):
        self.cards = cards
        self.exports = exports
        # Presets are files, not folders: NAME.preset.json, in the folder the game reads them from.
        self.presets = presets or os.path.join(os.path.dirname(cards), "presets")

    def folder(self, project):
        if not ID.match(project or ""):
            raise Refused(404, "No such card.")
        path = os.path.join(self.cards, project)
        if not os.path.isfile(os.path.join(path, "card.json")):
            raise Refused(404, "No such card.")
        return path

    def read(self, project):
        with open(os.path.join(self.folder(project), "card.json"), "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def write(self, project, card):
        """Saves the card and returns what the game's checks say about it."""
        if not isinstance(card, dict):
            raise Refused(400, "A card must be a JSON object.")
        path = os.path.join(self.folder(project), "card.json")
        with open(path + ".tmp", "wb") as f:
            f.write((json.dumps(card, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(path + ".tmp", path)
        return check_card(card)

    def list(self):
        found = []
        for name in sorted(os.listdir(self.cards)) if os.path.isdir(self.cards) else []:
            if not ID.match(name) or not os.path.isfile(os.path.join(self.cards, name, "card.json")):
                continue
            try:
                card = self.read(name)
                meta = card.get("meta", {}) if isinstance(card, dict) else {}
                found.append({"id": name, "title": meta.get("title") or name, "author": meta.get("author", ""),
                              "description": meta.get("description", ""), "problems": len(check_card(card))})
            except ValueError:
                found.append({"id": name, "title": name, "author": "", "description": "card.json is not valid JSON.", "problems": 1})
        return found

    def create(self, card):
        """Makes a folder for a new card, named after its id, and returns the folder name."""
        base, name, n = slug(card["meta"]["id"]), None, 1
        while name is None or os.path.exists(os.path.join(self.cards, name)):
            name = base if n == 1 else "%s_%d" % (base, n)
            n += 1
        card["meta"]["id"] = name
        os.makedirs(os.path.join(self.cards, name))
        with open(os.path.join(self.cards, name, "card.json"), "wb") as f:
            f.write(b"{}")
        self.write(name, card)
        return name

    def trash(self, project):
        """Moves a card out of the workspace instead of deleting it, so a slip can be undone by hand."""
        bin_ = os.path.join(self.cards, ".trash")
        if not os.path.isdir(bin_):
            os.makedirs(bin_)
        os.rename(self.folder(project), os.path.join(bin_, "%s-%d" % (project, int(time.time()))))

    def asset_path(self, project, relative):
        if not ASSET.match(relative or "") or ".." in relative.split("/") or os.path.splitext(relative)[1].lower() not in IMAGE_TYPES:
            raise Refused(400, "Assets are PNG, JPEG or WebP images kept under assets/, with plain names.")
        return os.path.join(self.folder(project), *relative.split("/"))

    def save_asset(self, project, relative, data):
        path = self.asset_path(project, relative)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(data)

    def pack(self, project):
        card = self.read(project)
        problems = check_card(card)
        if problems:
            raise Refused(409, "Fix the card's problems before packing it.")
        if not os.path.isdir(self.exports):
            os.makedirs(self.exports)
        name = card["meta"]["id"] + EXTENSION
        target = pack_card(self.folder(project), os.path.join(self.exports, name))
        return {"file": name, "size": os.path.getsize(target), "folder": self.exports}


    # Presets. "default" is the game's own: it can be read and copied here, never changed, so
    # there is always a known-good preset to go back to.

    def preset_path(self, preset_id, must_exist=True):
        if not ID.match(preset_id or ""):
            raise Refused(404, "No such preset.")
        path = os.path.join(self.presets, preset_id + PRESET_ENDING)
        if must_exist and not os.path.isfile(path):
            raise Refused(404, "No such preset.")
        return path

    def read_preset(self, preset_id):
        with open(self.preset_path(preset_id), "rb") as f:
            try:
                return json.loads(f.read().decode("utf-8"))
            except ValueError:
                raise Refused(409, "That preset file is not valid JSON.")

    def list_presets(self):
        found = []
        for name in sorted(os.listdir(self.presets)) if os.path.isdir(self.presets) else []:
            preset_id = name[:-len(PRESET_ENDING)]
            if not name.endswith(PRESET_ENDING) or not ID.match(preset_id):
                continue
            try:
                preset = self.read_preset(preset_id)
                found.append({"id": preset_id, "name": preset.get("name") or preset_id, "description": preset.get("description", ""),
                              "problems": len(check_preset(preset)), "builtin": preset_id == "default"})
            except Refused as e:
                found.append({"id": preset_id, "name": preset_id, "description": str(e), "problems": 1, "builtin": preset_id == "default"})
        return sorted(found, key=lambda p: (not p["builtin"], p["name"].lower()))

    def write_preset(self, preset_id, preset):
        """Saves the preset and returns what the game's checks say about it."""
        if preset_id == "default":
            raise Refused(403, "This is the game's own preset. Make a copy and change that.")
        if not isinstance(preset, dict):
            raise Refused(400, "A preset must be a JSON object.")
        path = self.preset_path(preset_id)
        with open(path + ".tmp", "wb") as f:
            f.write((json.dumps(preset, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(path + ".tmp", path)
        return check_preset(preset)

    def create_preset(self, name, source=None):
        """Makes a new preset as a copy of another (the game's own unless one is named) and returns its id."""
        name = (name or "").strip() or "My preset"
        if source:
            preset = self.read_preset(source)
        else:
            with open(GAME_PRESET, "rb") as f:
                preset = json.loads(f.read().decode("utf-8"))
        base, preset_id, n = slug(name, "preset"), None, 1
        while preset_id is None or preset_id == "default" or os.path.exists(self.preset_path(preset_id, must_exist=False)):
            preset_id = base if n == 1 else "%s_%d" % (base, n)
            n += 1
        preset["name"] = name
        if not os.path.isdir(self.presets):
            os.makedirs(self.presets)
        with open(self.preset_path(preset_id, must_exist=False), "wb") as f:
            f.write(b"{}")
        self.write_preset(preset_id, preset)
        return preset_id

    def trash_preset(self, preset_id):
        if preset_id == "default":
            raise Refused(403, "The game's own preset stays.")
        bin_ = os.path.join(self.presets, ".trash")
        if not os.path.isdir(bin_):
            os.makedirs(bin_)
        os.rename(self.preset_path(preset_id), os.path.join(bin_, "%s-%d%s" % (preset_id, int(time.time()), PRESET_ENDING)))


class Handler(BaseHTTPRequestHandler):
    workspace = None
    server_version = "SIStationCreator"

    def log_message(self, *args):
        pass

    # Replies.

    def send(self, status, body, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200):
        self.send(status, json.dumps(value).encode("utf-8"), "application/json")

    def send_file(self, path, content_type, extra=None):
        try:
            with open(path, "rb") as f:
                self.send(200, f.read(), content_type, extra)
        except (IOError, OSError):
            raise Refused(404, "Not found.")

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise Refused(413, "That file is too large (the limit is 50 MB).")
        return self.rfile.read(length)

    def json_body(self):
        try:
            return json.loads(self.body().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refused(400, "The request was not valid JSON.")

    # Routing.

    def handle_request(self, method):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        query = dict((k, v[0]) for k, v in parse_qs(url.query).items())
        try:
            if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
                raise Refused(403, "This app only answers on this computer.")
            self.route(method, parts, query)
        except Refused as e:
            self.send_json({"error": str(e)}, e.status)
        except CardError as e:
            self.send_json({"error": e.problems[0]}, 400)
        except sillytavern.ImportError_ as e:
            self.send_json({"error": str(e)}, 400)

    def route(self, method, parts, query):
        ws = self.workspace
        if parts[:1] != ["api"]:
            return self.static(method, parts)
        parts = parts[1:]

        if parts == ["templates"] and method == "GET":
            return self.send_json({"rules": RULES, "states": list(BUILTIN_STATES), "capabilities": list(CAPABILITIES)})
        if parts == ["projects"] and method == "GET":
            return self.send_json({"projects": ws.list(), "folder": ws.cards})
        if parts == ["projects"] and method == "POST":
            wanted = self.json_body()
            title = (wanted.get("title") or "").strip() or "Untitled Card"
            return self.send_json({"id": ws.create(new_card(wanted.get("template"), title))})
        if parts == ["convert", "lorebook"] and method == "POST":
            # Only converts. The editor adds the entries to the card it has open, so nothing it has unsaved is lost.
            return self.send_json({"entries": sillytavern.convert_lorebook(self.body())})
        if parts == ["import", "sillytavern"] and method == "POST":
            card, avatar = sillytavern.convert(self.body())
            project = ws.create(card)
            if avatar:
                # ws.create may have renamed the id to keep it unique; the asset paths follow the character id, which is unchanged.
                ws.save_asset(project, card["characters"][0]["sprites"]["neutral"], avatar)
            return self.send_json({"id": project})

        if parts == ["presets"] and method == "GET":
            return self.send_json({"presets": ws.list_presets(), "folder": ws.presets, "wording": BUILTIN_PROMPTS, "slots": SLOT_NOTES})
        if parts == ["presets"] and method == "POST":
            wanted = self.json_body()
            return self.send_json({"id": ws.create_preset(wanted.get("name"), wanted.get("copy_of"))})
        if parts == ["presets", "import"] and method == "POST":
            # A preset file somebody shared (or one exported here), added beside the others. Nothing is overwritten.
            try:
                return self.send_json({"id": take_preset(self.body(), query.get("name", ""), ws.presets)})
            except PresetError as e:
                raise Refused(400, str(e))
        if parts == ["preview"] and method == "POST":
            # Shows the preset builder what a prompt looks like when it goes out. The preset is sent along, so unsaved edits count.
            wanted = self.json_body()
            preset = wanted.get("preset")
            if not isinstance(preset, dict) or check_preset(preset):
                raise Refused(409, "Fix the preset's problems first; the game cannot build a prompt from it as it is.")
            card = load_card(ws.folder(wanted.get("card")))
            if wanted.get("turn"):
                return self.send_json(preview_turn(card, preset, wanted.get("provider") or "openrouter", wanted.get("bookkeeper", True) is not False, wanted.get("check") is True, wanted.get("header", True) is not False))
            return self.send_json(preview_prompt(card, preset, wanted.get("key")))
        if len(parts) == 3 and parts[0] == "presets" and parts[2] == "download" and method == "GET":
            # The preset as a file to hand to someone: they add it with Import a preset file on the game's Preset screen.
            ws.read_preset(parts[1])        # refuses a file that is not valid JSON
            return self.send_file(ws.preset_path(parts[1]), "application/json", {"Content-Disposition": 'attachment; filename="%s%s"' % (parts[1], PRESET_ENDING)})
        if len(parts) == 2 and parts[0] == "presets":
            if method == "GET":
                preset = ws.read_preset(parts[1])
                return self.send_json({"preset": preset, "problems": check_preset(preset), "builtin": parts[1] == "default"})
            if method == "PUT":
                return self.send_json({"problems": ws.write_preset(parts[1], self.json_body())})
            if method == "DELETE":
                ws.trash_preset(parts[1])
                return self.send_json({"ok": True})

        if len(parts) >= 2 and parts[0] == "projects":
            project, rest = parts[1], parts[2:]
            if not rest and method == "GET":
                card = ws.read(project)
                return self.send_json({"card": card, "problems": check_card(card)})
            if not rest and method == "PUT":
                return self.send_json({"problems": ws.write(project, self.json_body())})
            if not rest and method == "DELETE":
                ws.trash(project)
                return self.send_json({"ok": True})
            if rest == ["asset"] and method == "POST":
                ws.save_asset(project, query.get("path"), self.body())
                return self.send_json({"path": query.get("path")})
            if rest == ["pack"] and method == "POST":
                return self.send_json(ws.pack(project))
            if rest[:1] == ["file"] and method == "GET":
                relative = "/".join(rest[1:])
                return self.send_file(ws.asset_path(project, relative), IMAGE_TYPES[os.path.splitext(relative)[1].lower()])

        if len(parts) == 2 and parts[0] == "exports" and method == "GET":
            name = parts[1]
            if not name.endswith(EXTENSION) or not ID.match(name[:-len(EXTENSION)]):
                raise Refused(404, "Not found.")
            return self.send_file(os.path.join(ws.exports, name), "application/octet-stream",
                                  {"Content-Disposition": 'attachment; filename="%s"' % name})
        raise Refused(404, "Not found.")

    def static(self, method, parts):
        if method != "GET":
            raise Refused(405, "Not allowed.")
        name = "/".join(parts) or "index.html"
        kind = STATIC_TYPES.get(os.path.splitext(name)[1])
        if kind is None or ".." in parts:
            raise Refused(404, "Not found.")
        self.send_file(os.path.join(STATIC, *name.split("/")), kind)

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_DELETE(self):
        self.handle_request("DELETE")


def main():
    parser = argparse.ArgumentParser(description="SI-Station card creator")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--cards", default=os.path.join(ROOT, "cards"), help="folder of card folders to edit")
    parser.add_argument("--exports", default=os.path.join(ROOT, "exports"), help="where packed %s files are written" % EXTENSION)
    parser.add_argument("--presets", default=None, help="folder of preset files; by default the presets folder next to the cards folder, where the game reads them")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    Handler.workspace = Workspace(os.path.abspath(args.cards), os.path.abspath(args.exports), args.presets and os.path.abspath(args.presets))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    address = "http://127.0.0.1:%d" % args.port
    print("SI-Station card creator is running at %s (Ctrl+C to stop)" % address)
    print("Editing cards in %s" % Handler.workspace.cards)
    if not args.no_browser:
        webbrowser.open(address)

    # The page's own files are read fresh on every request, but the Python code is loaded once.
    # When it changes on disk, start again so new features work without anyone restarting by hand.
    loaded, stale = freshness.stamp(), threading.Event()

    def watch():
        while not stale.wait(1.0):
            if freshness.stamp() != loaded and freshness.loadable():
                stale.set()
                server.shutdown()

    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    if stale.is_set():
        print("The creator's code changed; restarting with the new version.")
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable] + [a for a in sys.argv if a != "--no-browser"] + ["--no-browser"])


if __name__ == "__main__":
    main()
