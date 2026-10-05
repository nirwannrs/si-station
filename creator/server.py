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

from aigame.card import BUILTIN_STATES, CAPABILITIES, EXTENSION, CardError, check_card, pack_card  # noqa: E402
import freshness  # noqa: E402
import sillytavern  # noqa: E402
from templates import RULES, new_card, slug  # noqa: E402

STATIC = os.path.join(HERE, "static")
ID = re.compile(r"^[a-z0-9_]+$")
ASSET = re.compile(r"^assets/[A-Za-z0-9_./-]+$")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
MAX_UPLOAD = 50 * 1024 * 1024


class Refused(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


class Workspace(object):
    """The folder of card folders being edited, and where packed cards go."""

    def __init__(self, cards, exports):
        self.cards = cards
        self.exports = exports

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
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    Handler.workspace = Workspace(os.path.abspath(args.cards), os.path.abspath(args.exports))
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
