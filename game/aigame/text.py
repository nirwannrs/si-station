"""Turns the light formatting people and models write (*italic*, **bold**) into Ren'Py text tags."""

import re

_RULES = (
    (re.compile(r"`([^`\n]+)`"), r"\1"),                                                   # `code` has no style here; drop the ticks
    (re.compile(r"(?m)^#{1,6}[ \t]+(.+)$"), r"{b}\1{/b}"),                                 # headings
    (re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"), r"{b}{i}\1{/i}{/b}"),
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"{b}\1{/b}"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), r"{b}\1{/b}"),
    (re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])"), r"{i}\1{/i}"),
    (re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)"), r"{i}\1{/i}"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"{s}\1{/s}"),
)
_TAG = re.compile(r"\{(/?)([bis])\}")


def escape(text):
    """Makes text safe to show as it is: Ren'Py would read [ and { as interpolation and text tags."""
    return text.replace("[", "[[").replace("{", "{{")


def _well_nested(tagged):
    open_tags = []
    for closing, name in _TAG.findall(tagged.replace("{{", "")):
        if not closing:
            open_tags.append(name)
        elif not open_tags or open_tags.pop() != name:
            return False
    return not open_tags


def to_renpy(text):
    """Escapes the text and applies its formatting marks. Marks that overlap in a way Ren'Py would
    reject are left as typed."""
    plain = escape(text)
    tagged = plain
    for pattern, replacement in _RULES:
        tagged = pattern.sub(replacement, tagged)
    return tagged if _well_nested(tagged) else plain


def _open_marks(piece):
    """Which of ** and * are left open at the end of a piece of text."""
    bold = piece.count("**") % 2 == 1
    italic = piece.replace("**", "").count("*") % 2 == 1
    return bold, italic


def keep_marks_paired(pieces):
    """When one formatted span was cut across several pieces, closes it at each cut and reopens it
    after, so every piece formats correctly by itself."""
    fixed, bold, italic = [], False, False
    for piece in pieces:
        piece = ("**" if bold else "") + ("*" if italic else "") + piece
        bold, italic = _open_marks(piece)
        fixed.append(piece + ("*" if italic else "") + ("**" if bold else ""))
    return fixed
