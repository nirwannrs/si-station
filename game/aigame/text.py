"""Turns the light formatting people and models write (*italic*, **bold**) into Ren'Py text tags."""

import random
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


# What a mouth that cannot form words lets out: hums, grunts and breath through the nose, built
# from the sounds a closed mouth can still make. There is never a vowel in it, since a vowel needs
# the mouth open.
_SOUNDS = ("mmph", "mmf", "mph", "hmph", "nngh", "mmh", "hmm", "mrph", "mmrf", "nnf", "hnn", "mnf", "ngh", "mmgh", "hmf", "mrrm",
           "nmph", "hnngh", "mrgh", "fmm", "gmph", "mhm", "nnh", "mrf", "hrm", "mnn", "rmph", "mmn", "grm", "hmn", "mff", "nph")
_SHORT = ("mm", "mh", "hm", "hn", "nn", "mf", "ng", "nh")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
_ACTED = re.compile(r"(\*+[^*\n]+\*+|\([^()\n]*\))")


def _muffle(word, pick, last):
    """One word as muffled sound, about as long as the word, and never the sound just made."""
    for attempt in range(8):
        sound = pick.choice(_SHORT if len(word) <= 2 else _SOUNDS)
        # A longer word is the same sound held longer: one of its hums or growls drawn out.
        held = [n for n, c in enumerate(sound) if c in "mnr"]
        if held and len(word) > len(sound):
            at = pick.choice(held)
            sound = sound[:at] + sound[at] * (min(len(word), 8) - len(sound)) + sound[at:]
        if sound != last:
            break
    return sound.upper() if len(word) > 1 and word.isupper() else sound.capitalize() if word[0].isupper() else sound


def muffled(text):
    """The message as it sounds from someone who cannot speak: (the text with what is spoken turned
    to muffled noise, the spoken pieces as they came out). What is spoken is whatever stands in
    double quotes; in a message with none that sets its actions in *asterisks*, it is everything
    outside them. A message that marks neither is left as typed, since nothing tells its speech
    from what its writer does. Every word becomes a different sound, picked at random but the same
    each time for the same message."""
    said = []
    # The same message always comes out the same, however often it is worked out, while each word
    # in it gets a sound of its own.
    pick, last = random.Random(text), [""]

    def word(match):
        last[0] = _muffle(match.group(0), pick, last[0].lower())
        return last[0]

    def sound(piece):
        out = _WORD.sub(word, piece)
        if out.strip() and out != piece:
            said.append(out.strip())
        return out

    if _QUOTED.search(text):
        shown = _QUOTED.sub(lambda m: m.group(0)[0] + sound(m.group(1) or m.group(2)) + m.group(0)[-1], text)
    elif re.search(r"\*[^*\n]+\*", text):
        shown = "".join(piece if n % 2 else sound(piece) for n, piece in enumerate(_ACTED.split(text)))
    else:
        shown = text
    return shown, said
