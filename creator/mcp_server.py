"""MCP server that lets an AI assistant (Claude Code, Claude Desktop, ...) make and edit SI-Station cards.

It speaks the Model Context Protocol over stdin/stdout and needs nothing beyond Python itself.
It works on the same card folders as the creator app and uses the game's own checks, so every
edit comes back with what the game would complain about.

    python3 creator/mcp_server.py [--cards FOLDER] [--exports FOLDER]
"""

import argparse
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "game"))
sys.path.insert(0, HERE)

from aigame.card import CardError, check_card  # noqa: E402
import freshness  # noqa: E402
import sillytavern  # noqa: E402
from server import IMAGE_TYPES, Refused, Workspace  # noqa: E402
from templates import new_card  # noqa: E402

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SCHEMA = os.path.join(ROOT, "spec", "card.schema.json")
MAX_IMAGE = 50 * 1024 * 1024

INSTRUCTIONS = """\
Tools for SI-Station cards: self-contained story games played in the SI-Station engine, where an AI narrates and the engine tracks state.
Call card_format once before writing card content; it returns the format every card must follow.
Change a card with edit_card, which takes small edits addressed by path; read only the part you need with get_card's path.
Every tool that changes a card returns "problems": the game's own checks, in plain words. A card is playable only when that list is empty, so fix what it lists before you finish.
Ids are lowercase letters, digits and underscores. Other parts of a card refer to things by id, so renaming an id means updating every reference to it."""


class ToolError(Exception):
    """Carries a message for the assistant: what went wrong and how to put it right."""


# Paths into a card: names separated by dots, list entries picked by id or position in brackets.
#   meta.title    rules.features.skills    characters[mira].personality    quests[missing_courier].stages[0].hint

_TOKEN = re.compile(r"([A-Za-z0-9_]+)|\[([^\]]+)\]")


def parse_path(path):
    if not isinstance(path, str) or not path.strip():
        raise ToolError("A path is needed, for example meta.title or characters[mira].personality.")
    tokens, position = [], 0
    for match in _TOKEN.finditer(path):
        gap = path[position:match.start()]
        if gap not in ("", "."):
            break
        tokens.append(("key", match.group(1)) if match.group(1) is not None else ("entry", match.group(2).strip()))
        position = match.end()
    if position != len(path) or not tokens:
        raise ToolError("Could not read the path %r. Use names separated by dots, with list entries in brackets: characters[mira].personality" % path)
    return tokens


def _entry_index(items, ref, path):
    if re.match(r"^-?\d+$", ref):
        index = int(ref)
        if -len(items) <= index < len(items):
            return index % len(items)
        raise ToolError("%s has %d entries; there is no [%s]." % (path, len(items), ref))
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("id") == ref:
            return index
    known = [i.get("id") for i in items if isinstance(i, dict) and i.get("id")]
    raise ToolError("%s has no entry with id %r. It has: %s." % (path, ref, ", ".join(known) or "none"))


def _step(node, token, walked):
    kind, name = token
    if kind == "key":
        if not isinstance(node, dict):
            raise ToolError("%s is not an object, so it has no %r." % (walked or "the card", name))
        if name not in node:
            raise ToolError("%s has no %r. It has: %s." % (walked or "The card", name, ", ".join(sorted(node)) or "nothing"))
        return node[name]
    if not isinstance(node, list):
        raise ToolError("%s is not a list, so [%s] does not apply." % (walked, name))
    return node[_entry_index(node, name, walked)]


def _show(tokens):
    return "".join("[%s]" % name if kind == "entry" else ("." if n else "") + name for n, (kind, name) in enumerate(tokens))


def read_path(card, path):
    node, tokens = card, parse_path(path)
    for n, token in enumerate(tokens):
        node = _step(node, token, _show(tokens[:n]))
    return node


def apply_edit(card, edit):
    """Applies one {"op", "path", "value"} to the card in place."""
    if not isinstance(edit, dict) or edit.get("op") not in ("set", "add", "remove"):
        raise ToolError('Each edit needs "op" ("set", "add" or "remove") and a "path".')
    op, tokens = edit["op"], parse_path(edit.get("path"))
    if op != "remove" and "value" not in edit:
        raise ToolError('A "%s" edit needs a "value".' % op)

    # Walk to the parent of the target. "set" and "add" create missing objects on the way down.
    node = card
    for n, token in enumerate(tokens[:-1]):
        if token[0] == "key" and isinstance(node, dict) and token[1] not in node and op != "remove":
            node[token[1]] = {}
        node = _step(node, token, _show(tokens[:n]))
    (kind, name), parent = tokens[-1], _show(tokens[:-1])

    if op == "add":
        if kind != "key" or not isinstance(node, dict):
            raise ToolError('"add" appends to a list, so its path must end in the list\'s name, like characters or quests[q1].stages.')
        target = node.setdefault(name, [])
        if not isinstance(target, list):
            raise ToolError("%s is not a list. Use \"set\" to replace it." % _show(tokens))
        value = edit["value"]
        if isinstance(value, dict) and value.get("id") and any(isinstance(i, dict) and i.get("id") == value["id"] for i in target):
            raise ToolError("%s already has an entry with id %r. Change that one with \"set\", or pick another id." % (_show(tokens), value["id"]))
        target.append(value)
    elif kind == "key":
        if not isinstance(node, dict):
            raise ToolError("%s is not an object, so it has no %r." % (parent or "the card", name))
        if op == "set":
            node[name] = edit["value"]
        elif name in node:
            del node[name]
        else:
            raise ToolError("%s has no %r to remove." % (parent or "The card", name))
    else:
        if not isinstance(node, list):
            raise ToolError("%s is not a list, so [%s] does not apply." % (parent, name))
        index = _entry_index(node, name, parent)
        if op == "set":
            node[index] = edit["value"]
        else:
            del node[index]


# The tools.

def _summary(project, card):
    problems = check_card(card)
    return {"card": project, "playable": not problems, "problems": problems}


class Tools(object):
    def __init__(self, workspace):
        self.ws = workspace

    def card_format(self, args):
        with open(SCHEMA, "rb") as f:
            schema = json.loads(f.read().decode("utf-8"))
        return {
            "schema": schema,
            "notes": [
                "rules.features switches the optional systems; anything off is neither tracked nor shown. Stats, quests, shops and the map exist when the card defines any.",
                "Turn-based fights (rules.battle.mode \"system\") need rules.battle.health_stat, and only characters defined in the card can be fought.",
                "Use {{user}} in world and character text wherever the player's name belongs.",
                "Image paths in the card (cover, sprites, backgrounds) must point at files added with add_image.",
            ],
        }

    def list_cards(self, args):
        return {"cards": self.ws.list(), "folder": self.ws.cards}

    def get_card(self, args):
        card = self.ws.read(args.get("card"))
        if args.get("path"):
            return {"card": args["card"], "path": args["path"], "value": read_path(card, args["path"])}
        return dict(_summary(args["card"], card), content=card)

    def create_card(self, args):
        title = (args.get("title") or "").strip()
        if not title:
            raise ToolError("A new card needs a title.")
        project = self.ws.create(new_card(args.get("template") or "blank", title))
        return dict(_summary(project, self.ws.read(project)), note="Created from the %s starting point. Fill it in with edit_card." % (args.get("template") or "blank"))

    def edit_card(self, args):
        project, edits = args.get("card"), args.get("edits")
        card = self.ws.read(project)
        if not isinstance(edits, list) or not edits:
            raise ToolError('"edits" must be a list of at least one edit.')
        for n, edit in enumerate(edits):
            try:
                apply_edit(card, edit)
            except ToolError as e:
                # All or nothing: an edit that fails leaves the saved card exactly as it was.
                raise ToolError("Edit %d of %d failed, so nothing was changed. %s" % (n + 1, len(edits), e))
        self.ws.write(project, card)
        return dict(_summary(project, card), applied=len(edits))

    def replace_card(self, args):
        project, content = args.get("card"), args.get("content")
        if not isinstance(content, dict):
            raise ToolError('"content" must be the whole card as an object.')
        self.ws.folder(project)
        self.ws.write(project, content)
        return _summary(project, content)

    def add_image(self, args):
        project, source, target = args.get("card"), args.get("file"), args.get("save_as")
        self.ws.folder(project)
        if not isinstance(source, str) or not os.path.isfile(source):
            raise ToolError("There is no file at %r. Give the full path of an image on this computer." % (source,))
        if os.path.splitext(source)[1].lower() not in IMAGE_TYPES or os.path.getsize(source) > MAX_IMAGE:
            raise ToolError("Images must be PNG, JPEG or WebP and under 50 MB.")
        with open(source, "rb") as f:
            self.ws.save_asset(project, target, f.read())
        return {"card": project, "saved_as": target, "note": "Now point the card at it, for example set characters[ID].sprites.neutral or meta.cover to this path."}

    def import_lorebook(self, args):
        project, source = args.get("card"), args.get("file")
        card = self.ws.read(project)
        if not isinstance(source, str) or not os.path.isfile(source):
            raise ToolError("There is no file at %r. Give the full path of a SillyTavern lorebook on this computer." % (source,))
        with open(source, "rb") as f:
            added = sillytavern.add_lore(card, sillytavern.convert_lorebook(f.read()))
        self.ws.write(project, card)
        return dict(_summary(project, card), added=added, note="Entries were appended to lorebook. Review them with get_card path \"lorebook\".")

    def pack_card(self, args):
        done = self.ws.pack(args.get("card"))
        return {"file": os.path.join(done["folder"], done["file"]), "size_bytes": done["size"]}

    def trash_card(self, args):
        self.ws.trash(args.get("card"))
        return {"moved_to": os.path.join(self.ws.cards, ".trash"), "note": "The card was moved, not deleted. Move its folder back to undo."}


_CARD = {"type": "string", "description": "The card's folder name, as given by list_cards."}

TOOLS = [
    {"name": "card_format", "description": "Returns the format every SI-Station card must follow (a JSON Schema plus notes). Call this once before writing or changing card content.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_cards", "description": "Lists the cards that can be edited, with how many problems each has.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_card", "description": "Reads a card. With no path, returns the whole card and its problems. With a path, returns just that part, which is cheaper for large cards.",
     "inputSchema": {"type": "object", "required": ["card"], "additionalProperties": False, "properties": {
         "card": _CARD,
         "path": {"type": "string", "description": "Optional. Names separated by dots, list entries by id or position in brackets: meta, rules.features, characters[mira], quests[q1].stages[0]."}}}},
    {"name": "create_card", "description": "Makes a new card and returns its folder name. The starting point only sets which systems are on; everything can be changed afterwards.",
     "inputSchema": {"type": "object", "required": ["title"], "additionalProperties": False, "properties": {
         "title": {"type": "string"},
         "template": {"enum": ["blank", "casual", "rpg"], "description": "casual: only relationships are tracked, for slice-of-life stories. rpg: health, mana, stamina, levels, skills, money and turn-based fights. blank: nothing switched either way."}}}},
    {"name": "edit_card", "description": "Changes a card with one or more small edits, applied together or not at all, then returns the game's checks on the result. "
                                         "set replaces or creates the value at a path. add appends an entry to a list. remove deletes a value or a list entry.",
     "inputSchema": {"type": "object", "required": ["card", "edits"], "additionalProperties": False, "properties": {
         "card": _CARD,
         "edits": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["op", "path"], "additionalProperties": False, "properties": {
             "op": {"enum": ["set", "add", "remove"]},
             "path": {"type": "string", "description": "Where to apply it. Examples: meta.title, world.opening, rules.features.skills, characters[mira].personality, items[stew].effects, and for add the list itself: characters, quests[q1].stages."},
             "value": {"description": "For set and add. Any JSON value: text, a number, true or false, an object or a list."}}}}}}},
    {"name": "replace_card", "description": "Replaces a card's whole content. Prefer edit_card; use this only for a full rewrite.",
     "inputSchema": {"type": "object", "required": ["card", "content"], "additionalProperties": False, "properties": {
         "card": _CARD, "content": {"type": "object", "description": "The complete card."}}}},
    {"name": "add_image", "description": "Copies an image from this computer into a card so the card can use it as a cover, sprite or background. Does not change the card itself.",
     "inputSchema": {"type": "object", "required": ["card", "file", "save_as"], "additionalProperties": False, "properties": {
         "card": _CARD,
         "file": {"type": "string", "description": "Full path of a PNG, JPEG or WebP file."},
         "save_as": {"type": "string", "description": "Where it goes inside the card. Use assets/cover.png, assets/sprites/CHARACTER_ID/EXPRESSION.png or assets/bg/LOCATION_ID.jpg."}}}},
    {"name": "import_lorebook", "description": "Adds the entries of a SillyTavern lorebook to a card's lorebook. Takes a World Info JSON file, or a character card (PNG or JSON) that carries a lorebook. Switched-off and empty entries are skipped.",
     "inputSchema": {"type": "object", "required": ["card", "file"], "additionalProperties": False, "properties": {
         "card": _CARD, "file": {"type": "string", "description": "Full path of the lorebook file."}}}},
    {"name": "pack_card", "description": "Seals a finished card into a .sicard file for players. Fails if the card still has problems.",
     "inputSchema": {"type": "object", "required": ["card"], "additionalProperties": False, "properties": {"card": _CARD}}},
    {"name": "trash_card", "description": "Moves a card to the trash folder. Only do this when the person has clearly asked for the card to be removed.",
     "inputSchema": {"type": "object", "required": ["card"], "additionalProperties": False, "properties": {"card": _CARD}}},
]


# The protocol: one JSON-RPC message per line on stdin, one per line on stdout.

def handle(tools, message):
    """Returns the reply to one message, or None for a notification."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None

    def reply(result):
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    if method == "initialize":
        wanted = (message.get("params") or {}).get("protocolVersion")
        return reply({"protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "si-station-cards", "version": "0.1.0", "code": CODE_ID}, "instructions": INSTRUCTIONS})
    if method == "ping":
        return reply({})
    if method == "tools/list":
        return reply({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in [t["name"] for t in TOOLS]:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unknown tool: %s" % name}}
        try:
            result, failed = getattr(tools, name)(args), False
        except (ToolError, Refused, sillytavern.ImportError_) as e:
            result, failed = {"error": str(e)}, True
        except CardError as e:
            result, failed = {"error": e.problems[0]}, True
        except (IOError, OSError, ValueError) as e:
            result, failed = {"error": "Could not do that: %s" % e}, True
        return reply({"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}], "isError": failed})
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found: %s" % method}}


# Which code this process is running: changes whenever a file it loaded is edited. Sent to the client
# on connecting, so a stale server can be told apart from a current one.
CODE_ID = "0"
PENDING = "SI_STATION_MCP_PENDING"


def restart_with_new_code(unhandled):
    """Replaces this process with a fresh one running the code now on disk. The client keeps the
    same connection and notices nothing; input already read but not yet answered is handed over."""
    with tempfile.NamedTemporaryFile(delete=False, prefix="si-station-mcp-") as handover:
        handover.write(unhandled)
    os.environ[PENDING] = handover.name
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    global CODE_ID
    parser = argparse.ArgumentParser(description="MCP server for editing SI-Station cards")
    parser.add_argument("--cards", default=os.path.join(ROOT, "cards"))
    parser.add_argument("--exports", default=os.path.join(ROOT, "exports"))
    parser.add_argument("--watch", action="append", default=[], help=argparse.SUPPRESS)   # extra files whose change restarts the server; for tests
    args = parser.parse_args()
    tools = Tools(Workspace(os.path.abspath(args.cards), os.path.abspath(args.exports)))
    loaded = freshness.stamp(args.watch)
    CODE_ID = "%x" % (hash(loaded) & 0xffffffff)

    # Input is read as raw bytes, not through sys.stdin, so that nothing is left behind in a hidden
    # buffer when the process replaces itself.
    unread = b""
    if os.environ.get(PENDING):
        try:
            with open(os.environ[PENDING], "rb") as f:
                unread = f.read()
            os.remove(os.environ[PENDING])
        except (IOError, OSError):
            pass
        del os.environ[PENDING]

    while True:
        while b"\n" not in unread:
            chunk = os.read(0, 65536)
            if not chunk:
                return
            unread += chunk
        line, unread = unread.split(b"\n", 1)
        if not line.strip():
            continue
        # os.execv keeps the connection only on Unix-like systems; elsewhere the server runs on with what it loaded.
        if os.name != "nt" and freshness.stamp(args.watch) != loaded and freshness.loadable(args.watch):
            restart_with_new_code(line + b"\n" + unread)
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            answer = handle(tools, message) if isinstance(message, dict) else None
        if answer is not None:
            sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
