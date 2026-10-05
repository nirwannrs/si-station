"""Lets the creator's long-running programs notice that their code has changed on disk.

The creator server and the MCP server both load the game's card code once and then run for hours.
Without this they keep answering with whatever they loaded, so a card that is fine by the current
code can be called broken by a server started before that code existed. Each of them checks this
stamp and starts itself again when it moves.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FOLDERS = (HERE, os.path.join(ROOT, "game", "aigame"))


def _files(extra=()):
    found = [f for f in extra if os.path.isfile(f)]
    for folder in FOLDERS:
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                found.append(os.path.join(folder, name))
    return found


def stamp(extra=()):
    """A value that changes whenever any of the Python files these programs load is edited, added or removed."""
    return tuple((path, os.stat(path).st_mtime_ns) for path in _files(extra))


def loadable(extra=()):
    """False while a file is half-written or has a syntax error, so a restart would only crash."""
    for path in _files(extra):
        if not path.endswith(".py"):
            continue
        try:
            with open(path, "rb") as f:
                compile(f.read(), path, "exec")
        except (SyntaxError, ValueError, IOError, OSError):
            return False
    return True
