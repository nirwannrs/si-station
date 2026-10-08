"""The journal: the story's long-term memory, kept scene by scene.

The story so far is sent to the model whole for as long as it fits. When it no longer does, the
oldest turns leave the prompt, and the journal is what keeps them. At the end of each scene a
helper writes an entry about it: a title, one sentence saying what happened, a fuller account,
and the words that should bring it back. Once the scene's turns have left the prompt:

- its one sentence is sent every turn, in a list of all that has happened, in order. That list is
  only ever added to, so nothing in it is ever squeezed out by what came after;
- its full account is sent the way a lorebook entry is: on the turns where one of its words comes
  up in what is being said, or was said in the last few turns, or when the player is back where
  it happened or with someone who was in it. An entry that does not matter this turn costs nothing.

A scene ends where the game can see it end: the player goes somewhere else, an objective of a
quest is finished, a fight is over. A scene that just goes on is closed after MAX_SCENE turns.
"""

import math
import re

from . import clock
from .card import PLAYER
from .llm import extract_json
from .state import named_in

MIN_SCENE = 3       # fewer turns than this are not worth an entry yet; they join the next scene
MAX_SCENE = 20      # a scene with no visible end is closed after this many turns
KEPT_BACK = 2       # ...leaving its newest turns for the next entry, since it is still going on
LONGEST = 30        # the most turns one entry is written from
RECALLED = 3        # the most entries sent in full in one turn when nothing points at one in particular, pinned ones aside
POINTED = 6         # ...and when something does: a word few scenes share
ROOM = 4500         # the most characters of full entries sent in one turn, pinned ones aside
LISTED = 60         # the most scenes in the list of what has happened, newest kept


def _quest_marks(state):
    return dict((quest, (progress["status"], progress["stage"])) for quest, progress in state.get("quests", {}).items())


def due(before, state, player_text=""):
    """Whether a journal entry should be written now that a turn has been played, and about which
    turns: (start, end, place) as positions in state["history"], end not included, with the place
    the scene happened in. None when the scene is still going on.

    before is the game state as it stood before the turn (what Undo keeps).
    """
    told = len(state["history"])
    start = min(state.get("journal_upto", 0), told)
    was_at, now_at = before["actors"][PLAYER]["location"], state["actors"][PLAYER]["location"]
    if was_at != now_at:
        end, place = told - 1, was_at               # the turn that arrives somewhere new opens the next scene
    elif _quest_marks(before) != _quest_marks(state) or player_text.startswith("(The fight is over"):
        end, place = told, now_at                   # the turn that finishes something closes its scene
    elif told - start >= MAX_SCENE:
        end, place = told - KEPT_BACK, now_at
    else:
        return None
    if end - start < MIN_SCENE:
        return None
    return max(start, end - LONGEST), end, place


def parse_entry(text):
    """What the helper wrote, as {"title", "gist", "content", "keywords"}, or None if it is unusable."""
    parsed = extract_json(text) or {}
    title, content, gist = parsed.get("title"), parsed.get("content"), parsed.get("gist")
    if not isinstance(content, str) or not content.strip():
        return None
    keywords = [k.strip() for k in parsed.get("keywords", []) if isinstance(k, str) and len(k.strip()) >= 3] if isinstance(parsed.get("keywords"), list) else []
    return {"title": " ".join(title.split())[:80] if isinstance(title, str) and title.strip() else "A scene",
            "gist": " ".join(gist.split())[:240] if isinstance(gist, str) else "",
            "content": " ".join(content.split())[:1200], "keywords": keywords[:12]}


def gist(entry):
    """The one sentence the scene is always remembered by: the helper's, or for an entry written
    before there was one, the first sentence of its account."""
    if entry.get("gist"):
        return entry["gist"]
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z\u00c0-\u024f\"'])", entry.get("content", "").strip())
    # A first sentence that is only the date ("Day 1, 11:39-12:16.") says nothing, and the list gives the date itself.
    said = first[1] if len(first) > 1 and len(first[0]) <= 40 and re.search(r"\d", first[0]) else first[0]
    return said if len(said) <= 240 else said[:237].rsplit(" ", 1)[0] + "..."


def add(card, state, start, end, place, written):
    """Files an entry about history[start:end]. The game adds what it knows for itself: who was
    named in those turns, and where it happened. Those bring the entry back later even when the
    helper's keywords are poor."""
    turns = state["history"][start:end]
    told = "\n".join("%s\n%s" % (t["player"], t["narration"]) for t in turns)
    entries = state.setdefault("journal", [])
    # The scene is given to the helper as "Player:" and "Narrator:", and it sometimes writes "the
    # player" for the person it is about. The story model reads these entries: they use the name.
    me = state["actors"][PLAYER]["name"]
    written = dict(written, title=by_name(written["title"], me), content=by_name(written["content"], me), gist=by_name(written.get("gist", ""), me))
    entry = dict(written, id=1 + max([e["id"] for e in entries] or [0]), start=start, end=end, place=place,
                 who=named_in(card, told + "\n" + written["content"]), pinned=False)
    when = [t["header"] for t in turns if t.get("header")]
    if when:
        entry["when"] = when[0].strip("[] ")        # the story's own time and place as the scene began
    if turns and all("there" in t or "with" in t for t in turns):
        # who was there for any of it, so the story model can tell who knows of it. See prompt.witnesses.
        entry["there"] = sorted(set(c for t in turns for c in t.get("there", t.get("with", []))))
    entries.append(entry)
    state["journal_upto"] = max(state.get("journal_upto", 0), end)
    return entry


def by_name(text, name):
    """The text with "the player" put as the player's character's name."""
    return re.sub(r"\b(?:[Tt]he player|Player)\b(?! character)", lambda m: name, text) if name else text


_COMMON = frozenset("with from into over that this then them they their there after before about what when where while first last".split())


def _terms(entry):
    """What brings an entry to mind, each with how much a mention of it counts: its keywords
    whole, and at half weight the longer single words of its keywords and its title, so that
    "Noelle" finds the scene filed under "Noelle Silva" and "race" the one titled "Broom race"."""
    terms = {}
    for keyword in entry.get("keywords", []):
        terms[keyword.lower()] = 1.0
    for phrase in entry.get("keywords", []) + [entry.get("title", "")]:
        for word in re.findall(r"[^\W\d_]{4,}", phrase.lower()):
            if word not in _COMMON:
                terms.setdefault(word, 0.5)
    return terms


def candidates(state):
    """The entries that can be brought back: those whose turns the story model no longer sees, and
    that are not pinned (a pinned entry is always sent)."""
    gone = state.get("summarized", 0)
    return [entry for entry in state.get("journal", []) if entry["end"] <= gone and not entry.get("pinned")]


def worth_asking(state):
    """Whether there are enough scenes out of sight that picking among them is a job for the memory
    helper. With only a few, the words that come up decide well enough."""
    return len(candidates(state)) > RECALLED


def recall(card, state, text, limit=None, recent="", picked=()):
    """The entries worth sending in full this turn. Only scenes whose turns the model no longer
    sees are candidates; a scene still in the prompt needs no reminder.

    They are brought back the way lorebook entries are, by their words turning up: in text, which
    is what is in play this turn, and at half strength in recent, the few turns before it, so that
    a subject stays in mind while it is being talked about and not only on the turn that names it.
    How many are sent depends on what was found. When a word that few scenes share comes up, the
    scenes it points at are sent, up to POINTED, and those that only share a common name with the
    text are left out. When there are only such common words to go by, the best RECALLED are sent.
    Either way no more than ROOM characters, so a long story cannot flood the prompt.

    picked is the ids of the entries the memory helper chose for this turn (see
    prompt.recall_prompt). Words can only find a scene that is spoken of in its own words; the
    helper reads the list of scenes and knows that "our duel at noon" is the broom race. What it
    picks comes first, and what the words find is added while there is room.

    An entry is relevant when the text in play (what the player typed, what the story just said)
    uses one of its keywords; when the player has just come back to where it happened; or when
    someone who was in it has just joined the player again. "Just" matters: a companion who never
    leaves, or a place the player never leaves, would otherwise bring up everything about them on
    every turn. Each turn records where the player was and who with when it began (see
    scene_facts), and that is what now is compared with. Keywords count most, being the most
    specific. Pinned entries are always sent and do not count against the limit.
    """
    gone = state.get("summarized", 0)
    me = state["actors"][PLAYER]
    present = set(who for who, actor in state["actors"].items() if who != PLAYER and actor["location"] is not None and actor["location"] == me["location"])
    last = state["history"][-1] if state["history"] else {}
    arrived = "place" in last and last["place"] != me["location"]
    joined = present - set(last["with"]) if "with" in last else set()
    low, earlier = text.lower(), recent.lower()
    pinned, found = [], []
    for entry in state.get("journal", []):
        if entry.get("pinned"):
            pinned.append(entry)
        elif entry["end"] <= gone:
            # A word also counts where it only begins one in the text: "duel" finds "dueled".
            hits = {}
            for term, weight in _terms(entry).items():
                said = r"(?<!\w)%s%s" % (re.escape(term), r"(?!\w)" if " " in term else r"\w{0,3}(?!\w)")
                if re.search(said, low):
                    hits[term] = weight
                elif earlier and re.search(said, earlier):
                    hits[term] = weight / 2.0
            found.append((entry, hits))
    # A word that many scenes share says little about which one is meant. A companion's name is in
    # nearly every entry and must not crowd out the one scene that has the subject in it, so a
    # word is worth less the larger the share of the entries it is found in.
    shared = {}
    for entry, hits in found:
        for term in hits:
            shared[term] = shared.get(term, 0) + 1
    scored = []
    for entry, hits in found:
        score = sum(3.0 * weight * math.log(1.0 + float(len(found)) / shared[term]) / math.log(1.0 + len(found)) for term, weight in hits.items())
        score += (2 if arrived and entry.get("place") and entry["place"] == me["location"] else 0) + (1 if joined & set(entry.get("who", [])) else 0)
        if entry["id"] in picked:
            score += 6
        if score:
            scored.append((score, entry["end"], entry))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    best = scored[0][0] if scored else 0
    if limit is None:
        limit = POINTED if best >= 1.5 else RECALLED
    chosen, room = [], ROOM
    for score, end, entry in scored[:limit]:
        if score < 0.4 * best or (chosen and len(entry["content"]) > room):
            continue
        chosen.append(entry)
        room -= len(entry["content"])
    return pinned + sorted(chosen, key=lambda e: e["end"])


def scene_facts(state):
    """Where the player is and who is with them, to keep with a turn: {"place", "with"}. Taken
    from the state as it stood when the turn began, so that the next turn can tell an arrival
    and a reunion from simply still being there."""
    me = state["actors"][PLAYER]
    return {"place": me["location"],
            "with": sorted(who for who, actor in state["actors"].items() if who != PLAYER and actor["location"] is not None and actor["location"] == me["location"])}


def scene_line(card, entry):
    """One scene in one line: when it was, what it is called, the sentence it is remembered by,
    and who of the people named in it was there."""
    read = clock.read(entry.get("when", ""))
    when = ", ".join(part for part in ("Day %d" % read["day"] if read["day"] is not None else "",
                                       "%02d:%02d" % (read["minutes"] // 60, read["minutes"] % 60) if read["minutes"] is not None else "") if part)
    seen = [card.characters[c]["name"] for c in entry.get("who", []) if c in card.characters and c in entry.get("there", entry.get("who", []))]
    return "%s%s. %s%s" % (when + ": " if when else "", entry["title"].rstrip("."), gist(entry), " (there: %s)" % ", ".join(seen[:8]) if seen else "")


def timeline(card, state):
    """Everything that has happened, scene by scene, oldest first: for each scene whose turns the
    story model no longer sees, when it was, what it is called, the one sentence it is remembered
    by, and who of the people named in it was there. It is sent every turn, and it only grows.

    This is the part of the memory that cannot miss. The running summary is rewritten each time
    turns are folded into it and loses what it has no room for; a full entry comes back only when
    one of its words comes up, and the player may call a race a duel. Without this list, someone
    who stood and watched a scene could be written as never having heard of it."""
    gone = state.get("summarized", 0)
    lines = ["- " + scene_line(card, entry) for entry in state.get("journal", []) if entry["end"] <= gone]
    if not lines:
        return ""
    return ("[What has happened so far, scene by scene]\n%s\nAll of these happened, and whoever is named with a scene was there and remembers it. "
            "Of a scene that is not told in full below you have only this much: do not make up more of what happened in it, and have nobody who was there deny it or speak as though it never took place." % "\n".join(lines[-LISTED:]))


def describe(entries, card=None):
    """Entries as the story model reads them, oldest first, each saying who was there for it."""
    if not entries:
        return ""
    def there(entry, e_when):
        names = [card.characters[c]["name"] for c in entry.get("there", []) if c in card.characters] if card is not None and "there" in entry else None
        facts = ([e_when] if e_when else []) + ([] if names is None else ["present: %s" % (", ".join(names) or "nobody but {{user}}")])
        return " (%s)" % "; ".join(facts) if facts else ""
    return "[Remembered from earlier in the story]\n" + "\n".join("- %s%s: %s" % (e["title"], there(e, e.get("when")), e["content"]) for e in entries)
