"""The journal: an entry per scene, sent back to the story model later when it matters.

Run with: python3 -m unittest discover tests
"""

import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))

from aigame import journal, prompt  # noqa: E402
from aigame.actions import apply_actions  # noqa: E402
from aigame.card import load_card  # noqa: E402
from aigame.state import new_game, reconcile  # noqa: E402


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            self.preset = json.load(f)

    def play(self, text, narration, actions=(), by_player=False):
        """One turn, and what the journal makes of it: the scene to write up, or None."""
        before = copy.deepcopy(self.state)
        apply_actions(self.card, self.state, list(actions), by_player=by_player)
        self.state["history"].append(dict({"player": text, "results": [], "narration": narration}, **journal.scene_facts(before)))
        return journal.due(before, self.state, text)

    def test_a_scene_ends_where_the_game_can_see_it_end(self):
        for n in range(4):
            self.assertIsNone(self.play("I chat with Mira.", "She pours ale."))             # the scene is going on
        self.assertEqual(self.play("I go out.", "Hay and horse.", [{"type": "move", "location": "stable"}], True), (0, 4, "common_room"))
        journal.add(self.card, self.state, 0, 4, "common_room", {"title": "Ale with Mira", "content": "Mira poured ale.", "keywords": ["ale"]})
        self.assertEqual(self.state["journal_upto"], 4)                                     # the turn that arrived opens the next scene
        self.assertIsNone(self.play("I look at the mare.", "She is lathered."))
        self.assertIsNone(self.play("I pat her.", "She calms."))
        # An objective finishing closes the scene, and that turn belongs to it.
        self.assertEqual(self.play("I ask Tobin.", "\"She came back alone.\"", [{"type": "quest_advance", "quest": "missing_courier"}]), (4, 8, "stable"))

    def test_short_scenes_wait_and_long_ones_are_closed(self):
        self.assertIsNone(self.play("I leave at once.", "Rain.", [{"type": "move", "location": "stable"}], True))      # nothing to write about yet
        for n in range(journal.MAX_SCENE - 2):
            self.assertIsNone(self.play("I wait.", "Nothing."))
        self.assertEqual(self.play("I wait.", "Nothing."), (0, journal.MAX_SCENE - journal.KEPT_BACK, "stable"))        # its newest turns are left for the next entry
        self.state["journal_upto"] = 0
        self.state["history"] += [{"player": "x", "results": [], "narration": "y"}] * 40
        start, end, place = self.play("(The fight is over.)", "You stand over him.")
        self.assertEqual((end - start, end), (journal.LONGEST, len(self.state["history"])))                            # never more than LONGEST turns in one entry

    def test_what_the_helper_wrote_is_cleaned_up(self):
        entry = journal.parse_entry('```json\n{"title": " The  letter ", "content": "Tobin gave\\nup the letter.", "keywords": ["letter", "ok", 5, " Tobin "]}\n```')
        self.assertEqual(entry, {"title": "The letter", "content": "Tobin gave up the letter.", "keywords": ["letter", "Tobin"]})
        self.assertEqual(journal.parse_entry('{"content": "Something happened."}')["title"], "A scene")
        for bad in ("no json", '{"title": "x"}', '{"content": "  "}'):
            self.assertIsNone(journal.parse_entry(bad))

    def entries(self):
        self.state["history"] = [{"player": "p%d" % n, "results": [], "narration": "n%d" % n} for n in range(12)]
        self.state["history"][1]["narration"] = "Tobin swears he never saw the courier."
        journal.add(self.card, self.state, 0, 4, "stable", {"title": "Tobin's lie", "content": "Tobin swore he never saw the courier.", "keywords": ["courier", "swore"]})
        journal.add(self.card, self.state, 4, 8, "cellar", {"title": "The cellar door", "content": "The cellar was locked; Mira keeps the key.", "keywords": ["cellar key", "locked"]})
        journal.add(self.card, self.state, 8, 12, "common_room", {"title": "A debt", "content": "The player promised to pay for the stew tomorrow.", "keywords": ["debt", "stew"]})

    def titles(self, text):
        return [e["title"] for e in journal.recall(self.card, self.state, text)]

    def test_an_entry_is_recalled_only_when_it_matters(self):
        self.entries()
        self.assertEqual(self.state["journal"][0]["who"], ["tobin"])                        # the game notes for itself who was in the scene
        self.assertEqual(self.titles("I ask about the courier."), [])                       # the model still sees those turns: no reminder needed
        self.state["summarized"] = 8                                                        # the first two scenes have left the prompt
        self.assertEqual(self.titles("I look around."), [])                                 # nothing in play touches them: nothing is sent
        self.assertEqual(self.titles("What about the courier?"), ["Tobin's lie"])           # a keyword
        self.assertEqual(self.titles("Is it LOCKED?"), ["The cellar door"])
        self.assertEqual(self.titles("I unlock everything."), [])                           # part of a word is not the word
        self.assertEqual(self.titles("I mention my debt."), [])                             # that scene is still in the prompt
        self.play("I go out to the stable.", "Tobin looks up.", [{"type": "move", "location": "stable"}], True)
        self.assertEqual(self.titles("I look around."), ["Tobin's lie"])                    # just back where it happened, and with who was there
        self.play("I say hello.", "He nods.")
        self.assertEqual(self.titles("I look around."), [])                                 # still there a turn later: it was said once, that is enough
        self.assertEqual(self.titles("About that courier."), ["Tobin's lie"])               # though a keyword still brings it up
        self.state["summarized"] = 12
        self.state["journal"][2]["pinned"] = True
        self.assertEqual(self.titles("I look around."), ["A debt"])                         # a pinned entry is always sent
        self.state["journal"] += [dict(self.state["journal"][0], id=10 + n, title="More %d" % n, end=4) for n in range(5)]
        self.assertEqual(len(journal.recall(self.card, self.state, "the courier")), 1 + journal.RECALLED)       # never a flood

    def test_the_scene_that_is_meant_is_found_among_scenes_that_share_a_name(self):
        """A companion is named in nearly every entry. The one scene with the subject in it must
        still come up, even when the player uses another word for it than the entry does."""
        me = self.state["actors"]["player"]["name"]
        self.state["history"] = [{"player": "p", "results": [], "narration": "n", "header": "[ 11:%02d | Day 1 ]" % n} for n in range(20)]
        for n, (title, keywords) in enumerate((("Mira's welcome", ["Mira", "kettle"]), ("Lunch and a yard duel", ["Mira", "Tobin", "boar bet"]),
                                               ("Broom race, silence bet", ["Mira Oakhand", "Tobin", "broom race"]), ("First dinner", ["Mira", "six cups"]), ("A quiet hour", ["Mira"]))):
            journal.add(self.card, self.state, n * 4, n * 4 + 4, "common_room", {"title": title, "content": "The player and Mira talked; the player's bet stands.", "keywords": keywords})
        self.state["summarized"] = 20
        self.assertEqual(self.state["journal"][0]["content"], "%s and Mira talked; %s's bet stands." % (me, me))     # filed under the name, not "the player"
        self.assertEqual(journal.by_name("the player character", me), "the player character")
        self.assertIn("Broom race, silence bet", self.titles("Mira, did you see how I raced her at noon?"))            # "raced" finds the race, over four other scenes with Mira in them
        self.assertEqual(self.titles("That duel at noon, Mira.")[-1:], ["A quiet hour"])                                # only her name to go by: the latest ones, as before
        self.assertIn("Lunch and a yard duel", self.titles("That duel at noon, Mira."))
        listed = journal.timeline(self.card, self.state)
        self.assertIn("\n- Day 1, 11:08: Broom race, silence bet (there: Mira Oakhand)\n", listed)                                             # and every scene is always named, with when it was
        self.assertEqual(listed.count("\n- "), 5)
        self.state["journal"] += [dict(self.state["journal"][0], id=50 + n, title="Scene %d" % n) for n in range(60)]
        self.assertEqual(journal.timeline(self.card, self.state).count("\n- "), journal.LISTED)                    # never an endless list

    def test_recalled_entries_travel_with_the_summary_and_not_in_the_cached_part(self):
        self.entries()
        self.state["summarized"], self.state["summary"] = 8, "The player came to the inn."
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "Who had the courier's letter?", [], record=False)
        last = messages[-1]["content"]
        self.assertIn("[Story so far]\nThe player came to the inn.\n\n[What has happened so far, scene by scene]\n- Tobin's lie (there: Tobin)\n- The cellar door (there: Mira Oakhand)\nAll of these happened", last)
        self.assertNotIn("A debt (", last)                                                   # that scene is still in the prompt: it needs no line
        self.assertIn("\n\n[Remembered from earlier in the story]\n- Tobin's lie: Tobin swore he never saw the courier.", last)
        self.assertNotIn("scene by scene", system)
        self.assertNotIn("Remembered from earlier", system)
        self.assertNotIn("The cellar was locked", last)                                               # only what this turn calls for
        quiet = prompt.narrator_prompt(self.card, self.state, self.preset, "I wait.", [], record=False)[1][-1]["content"]
        self.assertNotIn("Remembered from earlier", quiet)

    def test_an_older_save_starts_its_journal_from_now(self):
        old = dict(self.state, history=[{"player": "x", "results": [], "narration": "y"}] * 30)
        del old["journal"], old["journal_upto"]
        reconcile(self.card, old)
        self.assertEqual((old["journal"], old["journal_upto"]), ([], 30))


if __name__ == "__main__":
    unittest.main()
