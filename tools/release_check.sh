#!/bin/bash
# Checks a release before it is tagged: the unit tests, then the game's own self-test (see
# game/selftest.rpy) from source, then a fresh build of the downloads and the same self-test
# inside the built app for this computer.
#
#   tools/release_check.sh /path/to/renpy-sdk
#
# Everything happens in a temporary folder: the project is copied there, and the game is run with
# scratch folders for saves and data, so nothing here touches the project, real saves or settings.
# It exits with an error if any part fails. Tag a release only after it passes.

set -u
SDK="${1:-${RENPY_SDK:-}}"
[ -x "$SDK/renpy.sh" ] || { echo "Give the path of the Ren'Py SDK: tools/release_check.sh /path/to/renpy-sdk"; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
FAILED=0
trap 'rm -rf "$WORK"' EXIT

selftest() {    # selftest NAME COMMAND...   runs the game with the self-test switched on and prints its report
    local name="$1"; shift
    local report="$WORK/$name.json"
    rm -rf "$WORK/saves" "$WORK/data"
    SI_STATION_SELFTEST="$report" SI_STATION_DATA="$WORK/data" SDL_AUDIODRIVER=dummy "$@" --savedir "$WORK/saves" >"$WORK/$name.log" 2>&1 &
    local pid=$!
    for i in $(seq 1 120); do
        grep -q '"finished": true' "$report" 2>/dev/null && break
        kill -0 $pid 2>/dev/null || break
        sleep 1
    done
    kill $pid 2>/dev/null
    python3 - "$report" "$name" <<'PY' || FAILED=1
import json, sys
try:
    report = json.load(open(sys.argv[1]))
except Exception:
    print("%s: the game did not write a report (it did not start, or stopped early)" % sys.argv[2]); sys.exit(1)
for step in report["steps"]:
    print("  %s %s" % ("ok  " if step["ok"] else "FAIL", step["step"]))
    if not step["ok"]:
        print("       " + step["detail"].strip().splitlines()[-1])
print("%s: %s" % (sys.argv[2], "passed" if report["ok"] else "FAILED"))
sys.exit(0 if report["ok"] else 1)
PY
}

echo "== unit tests"
(cd "$SRC" && python3 -m unittest discover tests 2>&1 | tail -1) | tee "$WORK/unit.txt"
grep -q "^OK" "$WORK/unit.txt" || FAILED=1

echo "== copying the project"
rsync -a --exclude .git --exclude 'game/saves' --exclude 'game/cache' --exclude '*.rpyc' --exclude exports "$SRC/" "$WORK/project/"

echo "== self-test, from source"
selftest source "$SDK/renpy.sh" "$WORK/project" run

echo "== building the downloads"
find "$WORK/project" -name "*.rpyc" -delete
"$SDK/renpy.sh" "$SDK/launcher" distribute "$WORK/project" --destination "$WORK/dists" --package pc --package mac --no-update >"$WORK/build.log" 2>&1 || { echo "the build failed; last lines:"; tail -5 "$WORK/build.log"; FAILED=1; }
ls "$WORK/dists" 2>/dev/null | grep zip

echo "== what the downloads contain"
python3 - "$WORK/dists" <<'PY' || FAILED=1
import glob, sys, zipfile
bad = False
packages = sorted(glob.glob(sys.argv[1] + "/*.zip"))
if len(packages) != 2:
    print("  expected two packages, found %d" % len(packages)); bad = True
for package in packages:
    names = zipfile.ZipFile(package).namelist()
    for needed in ("cards/rusty_lantern/card.json", "cards/quiet_cafe/card.json", "presets/default.preset.json", "creator/server.py", "creator/static/app.js", "game/aigame/journal.py"):
        if not any(n.endswith("/" + needed) for n in names):
            print("  %s lacks %s" % (package.split("/")[-1], needed)); bad = True
    for personal in ("black_clover", "/tests/test_", "/game/saves/", "zz_smoke", "traceback.txt", ".mcp.json", "/.git/", "persistent"):
        found = [n for n in names if personal in n and "/lib/" not in n and "/renpy/" not in n]
        if found:
            print("  %s contains %s" % (package.split("/")[-1], found[0])); bad = True
print("  contents: %s" % ("FAILED" if bad else "ok"))
sys.exit(1 if bad else 0)
PY

echo "== self-test, inside the built app"
case "$(uname)" in
    Darwin) (cd "$WORK" && unzip -q dists/*-mac.zip -d app) && selftest built-mac "$WORK/app/SIStation.app/Contents/MacOS/SIStation" ;;
    Linux)  (cd "$WORK" && unzip -q dists/*-pc.zip -d app) && selftest built-linux "$(ls -d "$WORK"/app/*/)SIStation.sh" ;;
    *)      echo "  skipped: no built app for this system to try" ;;
esac

echo
[ $FAILED -eq 0 ] && echo "RELEASE CHECK PASSED" || echo "RELEASE CHECK FAILED"
exit $FAILED
