"""Reads the story's time and place line.

The story model writes the line and keeps it going (see prompt.py). The game does not own the
clock, but it can read it: the hour and the day number, where the line gives them in a form it
knows. That is enough to tell the story what part of the day it is, and to notice a clock that
stands still or runs backwards. A line in a form the game cannot read is left alone, never guessed at.
"""

import re

# The ways a card's own form of the line may write the hour, tried in this order: 18:13 and 6:13 PM;
# 6 PM; 1813 hours; 18h13 and 18.13. Each gives (hour, minute or None, a or p or None).
_HALF = r"\s*(?:([AaPp])\.?\s?[Mm]\b\.?)"
_TIMES = (
    re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])" + _HALF + "?"),
    re.compile(r"(?<![\d:.,])(\d{1,2})()" + _HALF),
    re.compile(r"(?<![\d:.,])([01]\d|2[0-3])([0-5]\d)()\s*(?:hours|hrs|h)\b", re.I),
    re.compile(r"(?<![\d:.,])(\d{1,2})[h.](\d{2})(?![\d.]|\s*\u00b0)" + _HALF + "?"),
)
_DAY = re.compile(r"\bDay\s*#?\s*(\d+)|\bD-?(\d+)\b", re.I)
# A line that gives the hour only as a word still says what part of the day it is.
_WORDS = (("midnight", 0), ("dawn", 5 * 60), ("sunrise", 6 * 60), ("morning", 9 * 60), ("noon", 12 * 60), ("midday", 12 * 60),
          ("afternoon", 15 * 60), ("sunset", 18 * 60), ("dusk", 18 * 60), ("evening", 20 * 60), ("night", 23 * 60 + 30))

# From which minute of the day each part of it runs, and what the hour means to people living in it.
_PARTS = (
    (0, "the middle of the night; almost everyone is asleep"),
    (4 * 60 + 30, "before dawn; only the earliest risers are up"),
    (6 * 60, "early morning; people are getting up, and it is time for a morning meal"),
    (9 * 60, "morning; the day's work is under way"),
    (11 * 60 + 30, "around midday; time for the midday meal"),
    (14 * 60, "afternoon"),
    (17 * 60, "early evening; the day's work is ending, and the evening meal is near"),
    (19 * 60, "evening; time for the evening meal and for rest after it"),
    (21 * 60 + 30, "late evening; people are turning in"),
    (23 * 60 + 30, "the middle of the night; almost everyone is asleep"),
)


def read(line):
    """{"minutes": minutes since midnight or None, "day": the day number or None} for a line."""
    found = {"minutes": None, "day": None}
    for form in _TIMES:
        time = form.search(line or "")
        if time:
            hour, minute, half = int(time.group(1)), int(time.group(2) or 0), (time.group(3) or "").lower()
            if half and 1 <= hour <= 12:
                hour = hour % 12 + (12 if half == "p" else 0)
            if hour < 24 and minute < 60:
                found["minutes"] = hour * 60 + minute
                break
    day = _DAY.search(line or "")
    if day:
        found["day"] = int(day.group(1) or day.group(2))
    return found


def went_back(before, now):
    """Whether the newer line is earlier than the older one. Only said when both give a day number
    and an hour: without the day, an hour that is lower may simply be past midnight."""
    b, n = read(before), read(now)
    if None in (b["minutes"], b["day"], n["minutes"], n["day"]):
        return False
    return (n["day"], n["minutes"]) < (b["day"], b["minutes"])


def same_moment(a, b):
    """Whether two lines give the same readable hour, on the same day where they give one."""
    a, b = read(a), read(b)
    return a["minutes"] is not None and a == b


def part_of_day(minutes):
    return [words for start, words in _PARTS if minutes >= start][-1]


def hour_line(line):
    """The hour in plain words, for the story model to be told beside the scene: "18:13 on Day 1:
    early evening; ...". None when the line gives no hour the game can read."""
    found = read(line)
    if found["minutes"] is None:
        # No hour in figures. A word for the part of the day ("Evening", "Dusk") is still worth saying back.
        # Only where the hour stands, at the head of the line: further on, "night" may be the weather or an era.
        low = re.split(r"[|/,]", (line or "").lower())[0]
        named = [minutes for word, minutes in _WORDS if re.search(r"\b%s\b" % word, low)]
        return part_of_day(named[0]) if len(named) == 1 else None
    return "%02d:%02d%s: %s" % (found["minutes"] // 60, found["minutes"] % 60, " on Day %d" % found["day"] if found["day"] is not None else "", part_of_day(found["minutes"]))


def place_part(line):
    """What the line says of the place: the part marked with the pin, without the pin. None when
    no part is marked so, as in a card's own form of the line that marks the place some other way."""
    for part in (line or "").strip("[] ").split("|"):
        if u"\U0001F4CD" in part:
            return part.replace(u"\U0001F4CD", "").strip()
    return None
