"""Loads and checks cards.

A card is a .sicard file or, while it is being made, a plain folder with card.json at its root.
A .sicard is that folder zipped and then sealed with the key below. Sealing keeps a card in one
file that ordinary tools do not open, and catches files that were damaged or edited afterwards.

It is a starting point, not strong protection. The key is part of this open-source code, so
anyone who reads the code can open any card. A fork that wants more can change seal() and
unseal() (and bump the container version in _MAGIC) without touching anything else: the rest of
the engine only ever sees the unsealed zip.
"""

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import zipfile

SPEC = "aigame-card"
SUPPORTED_MAJOR = "0"
PLAYER = "player"
SLOTS = ("head", "body", "hands", "feet", "weapon", "offhand", "accessory")
ITEM_TYPES = ("consumable", "equipment", "key", "misc")

# Systems a card can switch on or off in rules.features, with what they are when the card says nothing.
# Stats, quests, shops and the map need no switch: they exist when the card defines any.
FEATURES = {
    "inventory": True,
    "equipment": True,
    "money": True,
    "levels": False,
    "skills": False,
    "relationships": False,
    "states": False,
}

# Things a state can stop a character doing. "all" stands for every one of them.
CAPABILITIES = ("move", "items", "equipment", "skills", "trade", "attack")

# States every card has when states are on. A card can redefine these by id and add its own.
BUILTIN_STATES = (
    {"id": "asleep", "name": "Asleep", "description": "Sleeping. Unaware of the scene until woken.", "blocks": ["all"]},
    {"id": "unconscious", "name": "Unconscious", "description": "Knocked out. Cannot be woken by ordinary means.", "blocks": ["all"]},
    {"id": "restrained", "name": "Restrained", "description": "Tied up, held or locked in. Can still talk.", "blocks": ["all"]},
    {"id": "away", "name": "Away", "description": "Gone for a while. Not in the scene and cannot be spoken to.", "blocks": ["all"], "away": True},
)


class CardError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        Exception.__init__(self, "; ".join(self.problems))


class Card(object):
    """A checked card. Read-only during play; everything that changes lives in the game state."""

    def __init__(self, data, path, package=None):
        self.data = data
        self.path = path
        # The unsealed zip of a .sicard, kept in memory so assets never touch the disk. None for a folder card.
        self._package = package
        self.stats = _index(data["rules"]["stats"])
        self.items = _index(data.get("items"))
        self.characters = _index(data.get("characters"))
        self.locations = _index(data.get("locations"))
        self.shops = _index(data.get("shops"))
        self.quests = _index(data.get("quests"))
        self.skills = _index(data.get("skills"))
        self.states = _index(BUILTIN_STATES)
        self.states.update(_index(data.get("states")))

    def has(self, feature):
        """Whether the card uses a system. Equipment needs an inventory to draw from."""
        on = self.data["rules"].get("features", {}).get(feature, FEATURES[feature])
        return on and (feature != "equipment" or self.has("inventory"))

    @property
    def xp_per_level(self):
        return self.data["rules"].get("leveling", {}).get("xp_per_level", 100)

    @property
    def level_gains(self):
        return self.data["rules"].get("leveling", {}).get("gains", {})

    @property
    def battle(self):
        """The card's battle rules. Only meaningful when battle_system is true."""
        return self.data["rules"].get("battle", {})

    @property
    def battle_system(self):
        """True when the game runs fights turn by turn; false when the story simply tells them."""
        return self.battle.get("mode", "free") == "system"

    @property
    def relationship_name(self):
        return self.data["rules"].get("relationship_name", "Affection")

    @property
    def id(self):
        return self.data["meta"]["id"]

    @property
    def title(self):
        return self.data["meta"]["title"]

    @property
    def currency(self):
        return self.data["rules"].get("currency_name", "Gold")

    @property
    def visual(self):
        """False for cards made without assets, which play as a plain text log."""
        return self.data.get("display", {}).get("mode", "visual") == "visual"

    @property
    def allow_generated_items(self):
        return self.data["rules"].get("allow_generated_items", True)

    def read_asset(self, rel):
        if self._package is not None:
            with zipfile.ZipFile(io.BytesIO(self._package)) as z:
                return z.read(rel)
        parts = rel.split("/")
        if ".." in parts:
            raise KeyError(rel)
        with open(os.path.join(self.path, *parts), "rb") as f:
            return f.read()


def _index(objects):
    return dict((o["id"], o) for o in objects or [])


# The .sicard container: MAGIC, a 16-byte nonce, a 32-byte signature, then the encrypted zip.
# The last byte before the newline in MAGIC is the container version.
# _KEY is public, like the rest of this file: every copy of SI-Station uses the same one, which is
# what lets a card made anywhere be played anywhere.
EXTENSION = ".sicard"
_MAGIC = b"SICARD\x01\n"
_KEY = bytes.fromhex("0a6f28f8d994389ad59d20fd83c4b7bf9963c766466a1a8ba54d902b0bf0382e")
_SIGNING_KEY = hashlib.sha256(b"sicard signing" + _KEY).digest()


def _xor_stream(data, nonce):
    """Encrypts or decrypts: XOR with a SHAKE-256 keystream. Doing it as two big integers keeps it fast."""
    stream = hashlib.shake_256(_KEY + nonce).digest(len(data))
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def seal(plain):
    nonce = os.urandom(16)
    body = _xor_stream(plain, nonce)
    return _MAGIC + nonce + hmac.new(_SIGNING_KEY, nonce + body, hashlib.sha256).digest() + body


def unseal(blob):
    if not blob.startswith(_MAGIC):
        raise CardError(["This is not a %s card, or it was made for a newer version of the engine." % EXTENSION])
    nonce, signature, body = blob[8:24], blob[24:56], blob[56:]
    if not hmac.compare_digest(signature, hmac.new(_SIGNING_KEY, nonce + body, hashlib.sha256).digest()):
        raise CardError(["The card file is damaged or was changed after it was made."])
    return _xor_stream(body, nonce)


def pack_card(folder, target):
    """Turns a card folder into a .sicard file at target. The folder must pass the card checks."""
    load_card(folder)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(f for f in files if not f.startswith(".")):
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, folder).replace(os.sep, "/"))
    with open(target, "wb") as f:
        f.write(seal(buffer.getvalue()))
    return target


def load_card(path):
    package = None
    try:
        if os.path.isdir(path):
            with open(os.path.join(path, "card.json"), "rb") as f:
                raw = f.read()
        elif path.lower().endswith(EXTENSION):
            with open(path, "rb") as f:
                package = unseal(f.read())
            with zipfile.ZipFile(io.BytesIO(package)) as z:
                raw = z.read("card.json")
        else:
            raise CardError(["Cards are %s files." % EXTENSION])
        data = json.loads(raw.decode("utf-8"))
    except (IOError, OSError, KeyError, ValueError, zipfile.BadZipfile) as e:
        raise CardError(["Could not read the card at %s: %s" % (path, e)])
    problems = check_card(data)
    if problems:
        raise CardError(problems)
    return Card(data, path, package)


_cache = {}


def get_card(path):
    """load_card, cached by path. Saves store only the path, so the card is re-read after loading a save."""
    if path not in _cache:
        _cache[path] = load_card(path)
    return _cache[path]


def list_cards(library):
    """Every card in the library folder, as {"name", "card", "error"}. A card that fails its checks has card None."""
    try:
        names = sorted(os.listdir(library))
    except OSError:
        return []
    entries = []
    for name in names:
        path = os.path.join(library, name)
        is_folder = os.path.isfile(os.path.join(path, "card.json"))
        is_file = os.path.isfile(path) and name.lower().endswith(EXTENSION)
        if not (is_folder or is_file):
            continue
        try:
            entries.append({"name": name, "card": get_card(path), "error": None})
        except CardError as e:
            entries.append({"name": name, "card": None, "error": e.problems[0]})
    return entries


def import_card(source, library):
    """Checks a .sicard file and copies it into the library. Returns the name it is stored under.

    The stored name comes from the card's id, so importing a newer copy of a card replaces the old one.
    """
    if os.path.isdir(source):
        raise CardError(["Choose a %s file, not a folder." % EXTENSION])
    card = load_card(source)
    name = card.id + EXTENSION
    target = os.path.join(library, name)
    if not os.path.isdir(library):
        os.makedirs(library)
    if os.path.abspath(source) != os.path.abspath(target):
        shutil.copyfile(source, target)
    _cache.pop(target, None)
    return name


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _objects(data, key, problems):
    """The list at data[key] as dicts with unique string ids. Anything malformed is reported and dropped."""
    value = data.get(key, [])
    if not isinstance(value, list):
        problems.append("%s must be a list" % key)
        return []
    out, seen = [], set()
    for n, o in enumerate(value):
        if not isinstance(o, dict) or not isinstance(o.get("id"), str) or not o["id"]:
            problems.append("%s[%d] needs an id" % (key, n))
        elif o["id"] in seen:
            problems.append("%s has two entries with id %r" % (key, o["id"]))
        else:
            seen.add(o["id"])
            out.append(o)
    return out


def check_card(data):
    """Returns a list of problems; empty means the card is safe to play.

    Covers what the engine relies on: required fields and that every id reference resolves.
    """
    p = []
    if not isinstance(data, dict):
        return ["card.json must be an object"]
    if data.get("spec") != SPEC:
        p.append('spec must be "%s"' % SPEC)
    if str(data.get("spec_version", "")).split(".")[0] != SUPPORTED_MAJOR:
        p.append("unsupported spec_version %r (this game reads %s.x)" % (data.get("spec_version"), SUPPORTED_MAJOR))

    meta = data.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str) or not isinstance(meta.get("title"), str):
        p.append("meta needs an id and a title")
    elif not re.match(r"^[a-z0-9_]+$", meta["id"]):
        # The id becomes a file name when the card is imported.
        p.append("meta.id may only use lowercase letters, digits and underscores")
    world = data.get("world")
    if not isinstance(world, dict) or not isinstance(world.get("description"), str) or not isinstance(world.get("opening"), str):
        p.append("world needs a description and an opening")
        world = {}
    rules = data.get("rules")
    if not isinstance(rules, dict):
        p.append("rules is required")
        rules = {}

    display = data.get("display", {})
    if not isinstance(display, dict) or display.get("mode", "visual") not in ("visual", "text"):
        p.append('display.mode must be "visual" or "text"')

    features = rules.get("features", {})
    if not isinstance(features, dict) or any(k not in FEATURES or not isinstance(v, bool) for k, v in features.items()):
        p.append("rules.features may only switch %s on or off" % ", ".join(sorted(FEATURES)))

    stats = _objects(rules, "stats", p)
    skills = _objects(data, "skills", p)
    skill_ids = set(s["id"] for s in skills)
    items = _objects(data, "items", p)
    characters = _objects(data, "characters", p)
    locations = _objects(data, "locations", p)
    shops = _objects(data, "shops", p)
    quests = _objects(data, "quests", p)
    stat_ids = set(s["id"] for s in stats)
    item_by_id = _index(items)
    char_ids = set(c["id"] for c in characters)
    loc_ids = set(l["id"] for l in locations)

    for s in stats:
        if not _is_num(s.get("default")):
            p.append("stat %s needs a numeric default" % s["id"])

    states = _objects(data, "states", p)
    for s in states:
        if not isinstance(s.get("name"), str):
            p.append("state %s needs a name" % s["id"])
        blocks = s.get("blocks", [])
        if not isinstance(blocks, list) or any(b not in CAPABILITIES + ("all",) for b in blocks):
            p.append("state %s can only block: all, %s" % (s["id"], ", ".join(CAPABILITIES)))

    leveling = rules.get("leveling", {})
    if not isinstance(leveling, dict) or not _is_num(leveling.get("xp_per_level", 100)) or leveling.get("xp_per_level", 100) <= 0:
        p.append("rules.leveling.xp_per_level must be a number above zero")
    else:
        for stat, amount in leveling.get("gains", {}).items():
            if stat not in stat_ids or not _is_num(amount):
                p.append("rules.leveling.gains has unknown stat %r" % stat)

    battle = rules.get("battle", {})
    if not isinstance(battle, dict) or battle.get("mode", "free") not in ("free", "system"):
        p.append('rules.battle.mode must be "free" or "system"')
    elif battle.get("mode") == "system":
        if battle.get("health_stat") not in stat_ids:
            p.append("rules.battle.health_stat must name the stat that is health")
        for key in ("attack_stat", "defense_stat"):
            if battle.get(key) is not None and battle[key] not in stat_ids:
                p.append("rules.battle.%s is not a stat" % key)
        defeat = battle.get("on_defeat", {})
        if not isinstance(defeat, dict) or defeat.get("type", "survive") not in ("survive", "game_over") \
                or (defeat.get("location") is not None and defeat["location"] not in set(l["id"] for l in locations)):
            p.append('rules.battle.on_defeat needs type "survive" or "game_over" and, if given, a real location')

    for s in skills:
        if not isinstance(s.get("name"), str):
            p.append("skill %s needs a name" % s["id"])
        if s.get("target", "other") not in ("self", "other"):
            p.append('skill %s target must be "self" or "other"' % s["id"])
        for stat, amount in s.get("cost", {}).items():
            if stat not in stat_ids or not _is_num(amount) or amount < 0:
                p.append("skill %s has a cost in an unknown stat %r" % (s["id"], stat))
        for e in s.get("effects", []):
            if not isinstance(e, dict) or e.get("stat") not in stat_ids or not _is_num(e.get("amount")):
                p.append("skill %s has an effect with an unknown stat or non-numeric amount" % s["id"])

    for i in items:
        if i.get("type") not in ITEM_TYPES:
            p.append("item %s has unknown type %r" % (i["id"], i.get("type")))
        if i.get("type") == "equipment" and i.get("slot") not in SLOTS:
            p.append("equipment %s needs a slot (one of %s)" % (i["id"], ", ".join(SLOTS)))
        for e in i.get("effects", []):
            if not isinstance(e, dict) or e.get("stat") not in stat_ids or not _is_num(e.get("amount")):
                p.append("item %s has an effect with an unknown stat or non-numeric amount" % i["id"])

    def check_stacks(stacks, where):
        for s in stacks if isinstance(stacks, list) else []:
            if not isinstance(s, dict) or s.get("item") not in item_by_id:
                p.append("%s refers to an unknown item" % where)
            elif not isinstance(s.get("qty", 1), int) or s.get("qty", 1) < 1:
                p.append("%s has a bad qty for %s" % (where, s["item"]))

    def check_loadout(start, where):
        if start is None:
            return
        if not isinstance(start, dict):
            p.append("%s start must be an object" % where)
            return
        for stat, value in start.get("stats", {}).items():
            if stat not in stat_ids or not _is_num(value):
                p.append("%s start has unknown stat %r" % (where, stat))
        own, least = start.get("max", {}), start.get("min", {})
        for stat, most in (own.items() if isinstance(own, dict) else []):
            if stat not in stat_ids or not _is_num(most):
                p.append("%s start has a maximum for unknown stat %r" % (where, stat))
            elif _is_num(start.get("stats", {}).get(stat)) and start["stats"][stat] > most:
                p.append("%s starts with more %s (%s) than their own maximum (%s)" % (where, stat, start["stats"][stat], most))
        for stat, low in (least.items() if isinstance(least, dict) else []):
            if stat not in stat_ids or not _is_num(low):
                p.append("%s start has a minimum for unknown stat %r" % (where, stat))
            elif isinstance(own, dict) and _is_num(own.get(stat)) and low > own[stat]:
                p.append("%s has a minimum for %s (%s) above their own maximum (%s)" % (where, stat, low, own[stat]))
            elif _is_num(start.get("stats", {}).get(stat)) and start["stats"][stat] < low:
                p.append("%s starts with less %s (%s) than their own minimum (%s)" % (where, stat, start["stats"][stat], low))
        for s in start.get("states", []):
            if not isinstance(s, dict) or not isinstance(s.get("state"), str) or not s["state"]:
                p.append("%s start has a state with no name" % where)
        for s in start.get("skills", []):
            if not isinstance(s, dict) or s.get("skill") not in skill_ids:
                p.append("%s start has an unknown skill" % where)
        check_stacks(start.get("inventory"), "%s inventory" % where)
        for slot, item_id in start.get("equipment", {}).items():
            item = item_by_id.get(item_id)
            if slot not in SLOTS or item is None or item.get("slot") != slot:
                p.append("%s cannot start with %r in slot %r" % (where, item_id, slot))

    for key in ("header_format", "header_start"):
        if world.get(key) is not None and (not isinstance(world[key], str) or len(world[key]) > 400 or "\n" in world[key].strip()):
            p.append("world.%s must be one line of text, at most 400 characters" % key)
    if world.get("start_location") is not None and world["start_location"] not in loc_ids:
        p.append("world.start_location %r is not a location" % world["start_location"])

    for c in characters:
        if c["id"] == PLAYER:
            p.append('character id "player" is reserved')
        if not isinstance(c.get("name"), str):
            p.append("character %s needs a name" % c["id"])
        if c.get("location") is not None and c["location"] not in loc_ids:
            p.append("character %s is in unknown location %r" % (c["id"], c["location"]))
        check_loadout(c.get("start"), "character %s" % c["id"])

    persona = data.get("default_persona", {})
    if not isinstance(persona, dict):
        p.append("default_persona must be an object")
    else:
        check_loadout(persona.get("start"), "default_persona")

    for l in locations:
        for other in l.get("connections", []):
            if other not in loc_ids:
                p.append("location %s connects to unknown location %r" % (l["id"], other))

    for s in shops:
        if s.get("location") is not None and s["location"] not in loc_ids:
            p.append("shop %s is in unknown location %r" % (s["id"], s["location"]))
        if s.get("keeper") is not None and s["keeper"] not in char_ids:
            p.append("shop %s has unknown keeper %r" % (s["id"], s["keeper"]))
        check_stacks([{"item": e.get("item")} for e in s.get("stock", []) if isinstance(e, dict)], "shop %s stock" % s["id"])

    for q in quests:
        stages = q.get("stages")
        if not isinstance(stages, list) or not stages:
            p.append("quest %s needs at least one stage" % q["id"])
        for stage in stages if isinstance(stages, list) else []:
            conditions = stage.get("done_if", []) if isinstance(stage, dict) else []
            where = "quest %s objective %s" % (q["id"], stage.get("id") if isinstance(stage, dict) else "?")
            if not isinstance(conditions, list):
                p.append("%s: done_if must be a list of conditions" % where)
                continue
            for c in conditions:
                kind = c.get("type") if isinstance(c, dict) else None
                if kind not in ("has_item", "at", "level", "relationship"):
                    p.append("%s has a condition of unknown type %r (use has_item, at, level or relationship)" % (where, kind))
                    continue
                if c.get("who") is not None and c["who"] != PLAYER and c["who"] not in char_ids:
                    p.append("%s has a condition about unknown character %r" % (where, c["who"]))
                if kind == "has_item" and c.get("item") not in set(i["id"] for i in items):
                    p.append("%s has a condition about unknown item %r" % (where, c.get("item")))
                if kind == "at" and c.get("location") not in loc_ids:
                    p.append("%s has a condition about unknown location %r" % (where, c.get("location")))
                if kind == "relationship" and c.get("who") not in char_ids:
                    p.append("%s: a relationship condition must name a character" % where)
                if kind in ("level", "relationship") and (not isinstance(c.get("at_least"), (int, float)) or isinstance(c.get("at_least"), bool)):
                    p.append("%s: a %s condition needs a number in at_least" % (where, kind))
        if q.get("giver") is not None and q["giver"] not in char_ids:
            p.append("quest %s is given by unknown character %r" % (q["id"], q["giver"]))
        if q.get("start_location") is not None and q["start_location"] not in loc_ids:
            p.append("quest %s starts at unknown location %r" % (q["id"], q["start_location"]))
        if q.get("after") is not None and (q["after"] == q["id"] or q["after"] not in set(x["id"] for x in quests)):
            p.append("quest %s comes after unknown quest %r" % (q["id"], q["after"]))
        if q.get("after") is not None and q.get("auto_start"):
            p.append("quest %s cannot both be active from the start and come after another quest" % q["id"])
        rewards = q.get("rewards", {})
        check_stacks(rewards.get("items"), "quest %s rewards" % q["id"])
        for stat in rewards.get("stats", {}):
            if stat not in stat_ids:
                p.append("quest %s rewards unknown stat %r" % (q["id"], stat))

    return p
