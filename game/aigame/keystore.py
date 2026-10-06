"""Where API keys are kept.

A key must not sit in the game's folder or in its saved settings: those get zipped, synced, shared
and read by anything that scans a disk. So the game hands each key to the place the operating
system provides for secrets, and keeps nothing but the fact that a key exists.

    macOS     the login Keychain, through the system's own `security` tool
    Windows   a file only this Windows account can decrypt (DPAPI)
    Linux     the desktop's secret service, through `secret-tool`, when it is installed
    otherwise a private file outside the game folder, readable only by this user. It is scrambled
              so that a search for key-shaped text finds nothing, but it is not encrypted.

None of these stop a program already running as the same user that asks the system for the key
by name. What they stop is the key being found by reading files.
"""

import base64
import os
import subprocess
import sys

SERVICE = "SI-Station"
_fallback_dir = [None]


def setup(fallback_dir):
    """Where the private file goes on systems without a secret store. Never inside the game folder."""
    _fallback_dir[0] = fallback_dir


def _run(args, text=None):
    try:
        done = subprocess.run(args, input=text, capture_output=True, text=True, timeout=15)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""


# macOS. The secret is passed on standard input, never on the command line, where other
# programs could see it for as long as the command runs.

_SECURITY = "/usr/bin/security"


def _quoted(value):
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _mac_put(name, value):
    code, _ = _run([_SECURITY, "-i"], "add-generic-password -U -a %s -s %s -w %s\n" % (_quoted(name), _quoted(SERVICE), _quoted(value)))
    return code == 0 and _mac_get(name) == value


def _mac_get(name):
    code, out = _run([_SECURITY, "find-generic-password", "-a", name, "-s", SERVICE, "-w"])
    return out.rstrip("\n") if code == 0 else ""


def _mac_drop(name):
    _run([_SECURITY, "delete-generic-password", "-a", name, "-s", SERVICE])
    return True


# Linux, with a desktop secret service.

def _linux_put(name, value):
    code, _ = _run(["secret-tool", "store", "--label=%s %s" % (SERVICE, name), "service", SERVICE, "account", name], value)
    return code == 0 and _linux_get(name) == value


def _linux_get(name):
    code, out = _run(["secret-tool", "lookup", "service", SERVICE, "account", name])
    return out.rstrip("\n") if code == 0 else ""


def _linux_drop(name):
    _run(["secret-tool", "clear", "service", SERVICE, "account", name])
    return True


# A private file. On Windows its contents are encrypted for this account by the system; elsewhere
# they are only scrambled.

def _file_path():
    folder = _fallback_dir[0]
    if folder is None:
        if sys.platform == "win32":
            folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), SERVICE)
        elif sys.platform == "darwin":
            folder = os.path.join(os.path.expanduser("~"), "Library", "Application Support", SERVICE)
        else:
            folder = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "si-station")
    return os.path.join(folder, "keys")


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source, result = Blob(len(data), buffer), Blob()
    call = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise OSError("the system refused")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


_PAD = b"si-station keeps this out of plain sight; it is not encryption"


def _scramble(data):
    return bytes(b ^ _PAD[i % len(_PAD)] for i, b in enumerate(data))


def _file_read():
    try:
        with open(_file_path(), "rb") as f:
            raw = base64.b64decode(f.read())
        raw = _dpapi(raw, False) if sys.platform == "win32" else _scramble(raw)
        entries = {}
        for line in raw.decode("utf-8").split("\n"):
            if "\t" in line:
                name, value = line.split("\t", 1)
                entries[name] = value
        return entries
    except (OSError, ValueError):
        return {}


def _file_write(entries):
    try:
        path = _file_path()
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        raw = "\n".join("%s\t%s" % item for item in sorted(entries.items())).encode("utf-8")
        raw = _dpapi(raw, True) if sys.platform == "win32" else _scramble(raw)
        handle = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "wb") as f:
            f.write(base64.b64encode(raw))
        os.replace(path + ".tmp", path)
        return True
    except (OSError, ValueError):
        return False


def _file_put(name, value):
    entries = _file_read()
    entries[name] = value
    return _file_write(entries) and _file_read().get(name) == value


def _file_get(name):
    return _file_read().get(name, "")


def _file_drop(name):
    entries = _file_read()
    if name in entries:
        del entries[name]
        return _file_write(entries)
    return True


_FILE = (_file_put, _file_get, _file_drop)
_chosen = []


def _backend():
    """(kind, (put, get, drop)). Decided once: the system's own store if it works, else the file."""
    if not _chosen:
        if sys.platform == "darwin" and os.path.exists(_SECURITY):
            _chosen.append(("keychain", (_mac_put, _mac_get, _mac_drop)))
        elif sys.platform == "win32":
            _chosen.append(("windows", _FILE))
        elif sys.platform.startswith("linux") and _run(["secret-tool", "--version"])[0] == 0 and _run(["secret-tool", "search", "service", SERVICE])[0] is not None:
            _chosen.append(("secret-service", (_linux_put, _linux_get, _linux_drop)))
        else:
            _chosen.append(("file", _FILE))
    return _chosen[0]


def use(kind):
    """Forces a store. For tests."""
    del _chosen[:]
    if kind == "file":
        _chosen.append(("file", _FILE))


def kind():
    return _backend()[0]


def describe():
    """Where a key goes on this computer, for the player."""
    return {
        "keychain": "Kept in your macOS Keychain, not in the game's files.",
        "windows": "Kept encrypted for your Windows account, not in the game's files.",
        "secret-service": "Kept in your system's password store, not in the game's files.",
        "file": "Kept in a private file outside the game's folder. This system has no password store the game can use, so the file is scrambled but not encrypted.",
    }[kind()]


def put(name, value):
    """Stores a key, or removes it when value is empty. True if it is now stored as asked.
    If the system's store refuses, the private file is used instead, so a key is never lost."""
    if not value:
        return drop(name)
    if _backend()[1][0](name, value):
        return True
    return _backend()[1] is not _FILE and _file_put(name, value)


def get(name):
    return _backend()[1][1](name) or (_file_get(name) if _backend()[1] is not _FILE else "")


def drop(name):
    done = _backend()[1][2](name)
    if _backend()[1] is not _FILE:
        _file_drop(name)
    return done
