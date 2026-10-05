"""Packs a card folder into a .sicard file.

    python3 tools/pack_card.py cards/rusty_lantern            -> rusty_lantern.sicard next to the folder name
    python3 tools/pack_card.py cards/rusty_lantern out.sicard
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "game"))

from aigame.card import EXTENSION, CardError, pack_card  # noqa: E402


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    folder = argv[1].rstrip("/\\")
    target = argv[2] if len(argv) == 3 else os.path.basename(folder) + EXTENSION
    try:
        pack_card(folder, target)
    except CardError as e:
        sys.exit("This folder is not a valid card:\n- " + "\n- ".join(e.problems))
    print("Wrote %s (%d bytes)" % (target, os.path.getsize(target)))


if __name__ == "__main__":
    main(sys.argv)
