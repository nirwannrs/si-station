"""The journal: what happened in each scene, kept so the story model can be reminded of it later.

The story so far is sent to the model whole for as long as it fits. When it no longer does, the
oldest turns are folded into one short running summary, and their detail is gone from the prompt.
The journal is what keeps that detail reachable. At the end of each scene a helper writes a short
entry about it. Once the scene's turns have left the prompt, the entry is sent again on the turns
where it matters: when the player or the story mentions something it is about, when someone who
was in it is in the scene again, or when the player is back where it happened. An entry that does
not matter this turn costs nothing.

A scene ends where the game can see it end: the player goes somewhere else, an objective of a
quest is finished, a fight is over. A scene that just goes on is closed after MAX_SCENE turns.
"""

import re

from . import clock
from .card import PLAYER
from .llm import extract_json
from .state import named_in

MIN_SCENE = 3       # fewer turns than this are not worth an entry yet; they join the next scene
MAX_SCENE = 20      # a scene with no visible end is closed after this many turns
KEPT_BACK = 2       # ...leaving its newest turns for the next entry, since it is still going on
LONGEST = 30        # the most turns one entry is written from
RECALLED = 3        # the most entries sent in one turn, pinned ones aside
LISTED = 40         # the most scenes named in the list of what has happened, newest kept


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
    """What the helper wrote, as {"title", "content", "keywords"}, or None if it is unusable."""
    parsed = extract_json(text) or {}
    title, content = parsed.get("title"), parsed.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    keywords = [k.strip() for k in parsed.get("keywords", []) if isinstance(k, str) and len(k.strip()) >= 3] if isinstance(parsed.get("keywords"), list) else []
    return {"title": " ".join(title.split())[:80] if isinstance(title, str) and title.strip() else "A scene",
            "content": " ".join(content.split())[:1200], "keywords": keywords[:8]}


def add(card, state, start, end, place, written):
    """Files an entry about history[start:end]. The game adds what it knows for itself: who was
    named in those turns, and where it happened. Those bring the entry back later even when the
    helper's keywords are poor."""
    turns = state["history"][start:end]
    told = "\n".join("%s\n%s" % (t["player"], t["narration"]) for t in turns)
    entries = state.setdefault("journal", [])
    # The scene is given to the helper as "Player:" and "Narrator:", and it sometimes writes "the
    # player" for the person it is about. The story model reads these entries: they use the name.
    written = dict(written, title=by_name(written["title"], state["actors"][PLAYER]["name"]), content=by_name(written["content"], state["actors"][PLAYER]["name"]))
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
    return re.sub(r"\b[Tt]he player\b(?! character)", lambda m: name, text) if name else text


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


def recall(card, state, text, limit=RECALLED):
    """The entries worth sending this turn, most relevant first. Only scenes whose turns the model
    no longer sees are candidates; a scene still in the prompt needs no reminder.

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
    low = text.lower()
    pinned, found = [], []
    for entry in state.get("journal", []):
        if entry.get("pinned"):
            pinned.append(entry)
        elif entry["end"] <= gone:
            # A word also counts where it only begins one in the text: "duel" finds "dueled".
            found.append((entry, dict((term, weight) for term, weight in _terms(entry).items() if re.search(r"(?<!\w)%s%s" % (re.escape(term), r"(?!\w)" if " " in term else r"\w{0,3}(?!\w)"), low))))
    # A word that many scenes share says little about which one is meant. A companion's name is in
    # nearly every entry and must not crowd out the one scene that has the subject in it, so each
    # mention is worth less the more entries it brings up.
    shared = {}
    for entry, hits in found:
        for term in hits:
            shared[term] = shared.get(term, 0) + 1
    scored = []
    for entry, hits in found:
        score = sum(3.0 * weight / shared[term] for term, weight in hits.items())
        score += (2 if arrived and entry.get("place") and entry["place"] == me["location"] else 0) + (1 if joined & set(entry.get("who", [])) else 0)
        if score:
            scored.append((score, entry["end"], entry))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return pinned + sorted([entry for score, end, entry in scored[:limit]], key=lambda e: e["end"])


def scene_facts(state):
    """Where the player is and who is with them, to keep with a turn: {"place", "with"}. Taken
    from the state as it stood when the turn began, so that the next turn can tell an arrival
    and a reunion from simply still being there."""
    me = state["actors"][PLAYER]
    return {"place": me["location"],
            "with": sorted(who for who, actor in state["actors"].items() if who != PLAYER and actor["location"] is not None and actor["location"] == me["location"])}


def timeline(card, state):
    """Every scene whose turns the story model no longer sees, one line each, oldest first: when
    it was, what it is called, and who of the people named in it was there. It is sent every turn.

    Being reminded of a scene in full depends on something bringing it to mind, and that can miss:
    the player calls a race a duel, and the entry about the race stays unsent. Then someone who
    stood and watched it is written as never having heard of it. With this list the story model
    always knows the scene happened and who saw it, even when it is not given the details."""
    gone, lines = state.get("summarized", 0), []
    for entry in state.get("journal", []):
        if entry["end"] > gone:
            continue
        read = clock.read(entry.get("when", ""))
        when = ", ".join(part for part in ("Day %d" % read["day"] if read["day"] is not None else "",
                                           "%02d:%02d" % (read["minutes"] // 60, read["minutes"] % 60) if read["minutes"] is not None else "") if part)
        seen = [card.characters[c]["name"] for c in entry.get("who", []) if c in card.characters and c in entry.get("there", entry.get("who", []))]
        lines.append("- %s%s%s" % (when + ": " if when else "", entry["title"], " (there: %s)" % ", ".join(seen[:8]) if seen else ""))
    if not lines:
        return ""
    return ("[What has happened so far, scene by scene]\n%s\nAll of these happened, and whoever is named with a scene was there and remembers it. "
            "Where a scene is not told in full below, you have only its title: do not make up what happened in it, and have nobody who was there deny it or speak as though it never took place." % "\n".join(lines[-LISTED:]))


def describe(entries, card=None):
    """Entries as the story model reads them, oldest first, each saying who was there for it."""
    if not entries:
        return ""
    def there(entry, e_when):
        names = [card.characters[c]["name"] for c in entry.get("there", []) if c in card.characters] if card is not None and "there" in entry else None
        facts = ([e_when] if e_when else []) + ([] if names is None else ["present: %s" % (", ".join(names) or "nobody but {{user}}")])
        return " (%s)" % "; ".join(facts) if facts else ""
    return "[Remembered from earlier in the story]\n" + "\n".join("- %s%s: %s" % (e["title"], there(e, e.get("when")), e["content"]) for e in entries)
