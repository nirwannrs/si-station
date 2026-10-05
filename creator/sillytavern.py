"""Turns a SillyTavern character card (V1, V2 or V3; a PNG with embedded data, or plain JSON) into an SI-Station card.

A SillyTavern card describes one character and some lore. It has no items, stats, places or quests,
so the result is a casual card with that one character; the creator adds the rest in the editor.
"""

import base64
import json
import struct

from templates import RULES, slug

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImportError_(Exception):
    """Carries a message fit to show to the creator."""


def _png_text_chunks(blob):
    """keyword -> text for every tEXt chunk in a PNG."""
    found, offset = {}, len(PNG_SIGNATURE)
    while offset + 8 <= len(blob):
        length, kind = struct.unpack(">I4s", blob[offset:offset + 8])
        body = blob[offset + 8:offset + 8 + length]
        if kind == b"tEXt" and b"\x00" in body:
            keyword, text = body.split(b"\x00", 1)
            found[keyword.decode("latin-1")] = text
        offset += 12 + length
    return found


def read_card(blob):
    """Returns (the SillyTavern card as a dict, the PNG bytes if it came in one else None)."""
    image = None
    if blob.startswith(PNG_SIGNATURE):
        chunks = _png_text_chunks(blob)
        encoded = chunks.get("ccv3") or chunks.get("chara")
        if not encoded:
            raise ImportError_("This PNG has no character data in it. It may be a plain image, not a character card.")
        try:
            blob = base64.b64decode(encoded)
        except ValueError:
            raise ImportError_("The character data in this PNG is damaged.")
        image = True
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ImportError_("This file is not a SillyTavern card (PNG or JSON).")
    if not isinstance(data, dict):
        raise ImportError_("This file is not a SillyTavern card (PNG or JSON).")
    return data, image


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def convert(blob):
    """Returns (card dict, avatar PNG bytes or None)."""
    source, has_image = read_card(blob)
    avatar = blob if has_image else None
    data = source.get("data") if isinstance(source.get("data"), dict) else source   # V2/V3 nest under "data"; V1 is flat
    name = _text(data.get("name")) or "Unnamed"
    char_id = slug(name, "character")

    def fill(value):
        # {{char}} has no meaning in a multi-character card, so it becomes the character's name.
        return _text(value).replace("{{char}}", name).replace("<BOT>", name).replace("<USER>", "{{user}}")

    character = {"id": char_id, "name": name, "description": fill(data.get("description")) or "Imported from SillyTavern."}
    for ours, theirs in (("personality", "personality"), ("dialogue_examples", "mes_example")):
        if fill(data.get(theirs)):
            character[ours] = fill(data.get(theirs))
    character["start"] = {"relationship": 0}
    if avatar:
        character["sprites"] = {"neutral": "assets/sprites/%s/neutral.png" % char_id}

    meta = {"id": char_id, "title": name, "version": "0.1.0"}
    if _text(data.get("creator")):
        meta["author"] = _text(data.get("creator"))
    if _text(data.get("creator_notes")):
        meta["creator_note"] = _text(data.get("creator_notes"))
    if isinstance(data.get("tags"), list):
        meta["tags"] = [t for t in data["tags"] if isinstance(t, str)]
    if avatar:
        meta["cover"] = character["sprites"]["neutral"]

    scenario = fill(data.get("scenario"))
    world = {
        "description": scenario or "The world around %s." % name,
        "opening": fill(data.get("first_mes")) or "%s is here." % name,
    }
    if _text(data.get("system_prompt")) or _text(data.get("post_history_instructions")):
        world["narrator_instructions"] = "\n\n".join(t for t in (fill(data.get("system_prompt")), fill(data.get("post_history_instructions"))) if t)

    lorebook = []
    book = data.get("character_book") if isinstance(data.get("character_book"), dict) else {}
    for n, entry in enumerate(book.get("entries") if isinstance(book.get("entries"), list) else []):
        if not isinstance(entry, dict) or not fill(entry.get("content")) or entry.get("enabled") is False:
            continue
        item = {"id": "lore_%d" % (n + 1), "content": fill(entry.get("content"))}
        keys = [k for k in entry.get("keys") or [] if isinstance(k, str) and k.strip()]
        if keys:
            item["keys"] = keys
        if entry.get("constant") or not keys:
            item["always_on"] = True
        if isinstance(entry.get("priority"), int):
            item["priority"] = entry["priority"]
        lorebook.append(item)

    card = {
        "spec": "aigame-card", "spec_version": "0.1", "meta": meta, "world": world,
        "rules": json.loads(json.dumps(RULES["casual"])), "characters": [character],
    }
    if not avatar:
        card["display"] = {"mode": "text"}
    if lorebook:
        card["lorebook"] = lorebook
    return card, avatar


# Lorebooks. SillyTavern keeps these in two shapes: a World Info file, whose "entries" is an object
# keyed by number with fields named key / comment / order / disable, and the "character_book"
# inside a character card, whose "entries" is a list with keys / name / insertion_order / enabled.

def _find_book(source):
    """The lorebook inside whatever was given: a World Info file, a bare character_book, or a whole character card."""
    for candidate in (source, source.get("character_book"), (source.get("data") or {}).get("character_book") if isinstance(source.get("data"), dict) else None):
        if isinstance(candidate, dict) and isinstance(candidate.get("entries"), (dict, list)):
            return candidate
    raise ImportError_("There is no lorebook in this file. Choose a SillyTavern World Info file, or a character card that has a lorebook.")


def _keys(value):
    if isinstance(value, str):
        value = value.split(",")
    return [k.strip() for k in value or [] if isinstance(k, str) and k.strip()]


def convert_lorebook(blob):
    """Returns the file's entries in SI-Station's lorebook shape, each with a "title" to make an id from.
    Disabled and empty entries are left out."""
    source, from_image = read_card(blob)
    entries = _find_book(source)["entries"]
    if isinstance(entries, dict):
        entries = [entries[k] for k in sorted(entries, key=lambda k: (not str(k).isdigit(), int(k) if str(k).isdigit() else 0, str(k)))]
    found = []
    for entry in entries:
        if not isinstance(entry, dict) or not _text(entry.get("content")) or entry.get("disable") is True or entry.get("enabled") is False:
            continue
        item = {"title": _text(entry.get("comment")) or _text(entry.get("name")), "content": _text(entry.get("content")).replace("<USER>", "{{user}}")}
        keys = _keys(entry.get("key") if "key" in entry else entry.get("keys"))
        if keys:
            item["keys"] = keys
        if entry.get("constant") or not keys:
            item["always_on"] = True
        for field in ("priority", "order", "insertion_order"):
            if isinstance(entry.get(field), int) and not isinstance(entry.get(field), bool):
                item["priority"] = entry[field]
                break
        found.append(item)
    if not found:
        raise ImportError_("That lorebook has no usable entries: they are all empty or switched off.")
    return found


def add_lore(card, found):
    """Appends converted entries to the card's lorebook with ids that do not clash. Returns how many were added.

    {{char}} only means something when there is a single character to stand for it.
    """
    taken = set(e.get("id") for e in card.get("lorebook", []))
    only = card["characters"][0]["name"] if len(card.get("characters", [])) == 1 else None
    for item in found:
        base, name, n = slug(item.get("title") or "", "lore"), None, 1
        while name is None or name in taken:
            name = base if n == 1 and base != "lore" else "%s_%d" % (base, n)
            n += 1
        taken.add(name)
        entry = dict((k, v) for k, v in item.items() if k != "title")
        if only:
            entry["content"] = entry["content"].replace("{{char}}", only)
        card.setdefault("lorebook", []).append(dict(entry, id=name))
    return len(found)
