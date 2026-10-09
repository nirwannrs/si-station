"""Run with: python3 -m unittest discover tests"""

import copy
import json
import os
import pickle
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))

from aigame import battle, prompt  # noqa: E402
from aigame.actions import apply_action, apply_actions, sell_price  # noqa: E402
from aigame.card import Card, CardError, check_card, import_card, list_cards, load_card, pack_card  # noqa: E402
from aigame.state import describe_states, effective_stat, limits, new_game, reconcile  # noqa: E402

CARD_DIR = os.path.join(ROOT, "cards", "rusty_lantern")


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(CARD_DIR)
        self.state = new_game(self.card)
        self.me = self.state["actors"]["player"]

    def assertRaisesAndGet(self, kind, call, *args):
        with self.assertRaises(kind) as caught:
            call(*args)
        return caught.exception

    def do(self, **action):
        return apply_action(self.card, self.state, action)

    def ok(self, **action):
        result = self.do(**action)
        self.assertTrue(result["ok"], result["message"])
        return result["message"]

    def rejected(self, **action):
        before = copy.deepcopy(self.state)
        result = self.do(**action)
        self.assertFalse(result["ok"], result["message"])
        self.assertEqual(self.state, before, "a rejected action must not change the state")
        return result["message"]

    # card loading

    def test_new_game_uses_card_start_values(self):
        self.assertEqual(self.me["name"], "Traveler")
        self.assertEqual(self.me["money"], 6)
        self.assertEqual(self.me["stats"], {"hp": 20, "stamina": 5, "mana": 10, "defense": 0})
        self.assertEqual(self.me["inventory"], {"healing_draught": 1, "belt_knife": 1})
        self.assertEqual(self.me["equipment"], {"body": "travel_cloak"})
        self.assertEqual(self.state["actors"]["tobin"]["stats"]["hp"], 10)
        self.assertEqual(self.state["quests"], {"missing_courier": {"status": "active", "stage": 0, "met": []}})

    def test_persona_overrides_default_text_but_keeps_loadout(self):
        me = new_game(self.card, {"name": "Ash"})["actors"]["player"]
        self.assertEqual((me["name"], me["money"]), ("Ash", 6))
        self.assertEqual(me["description"], "A wanderer looking for work and a dry bed.")        # a part left out is the card's
        # The player's own persona, whole: nothing of the card's description is mixed in, and the starting kit is still the card's.
        own = new_game(self.card, {"name": "Ranma", "description": "", "appearance": "Red pigtail."})["actors"]["player"]
        self.assertEqual((own["name"], own["description"], own["appearance"]), ("Ranma", "", "Red pigtail."))
        self.assertEqual((own["money"], own["inventory"], own["equipment"]["body"]), (6, {"healing_draught": 1, "belt_knife": 1}, "travel_cloak"))

    def test_display_mode(self):
        self.assertTrue(self.card.visual)
        with open(os.path.join(CARD_DIR, "card.json")) as f:
            data = json.load(f)
        data["display"] = {"mode": "text"}
        self.assertEqual(check_card(data), [])
        data["display"] = {"mode": "hologram"}
        self.assertEqual(len(check_card(data)), 1)

    def test_import_and_library(self):
        import shutil, tempfile
        work = tempfile.mkdtemp()
        try:
            packed = pack_card(CARD_DIR, os.path.join(work, "download.sicard"))
            library = os.path.join(work, "library")
            self.assertEqual(import_card(packed, library), "rusty_lantern.sicard")
            self.assertEqual(import_card(packed, library), "rusty_lantern.sicard")  # importing again replaces it
            plain_zip = shutil.make_archive(os.path.join(library, "plain"), "zip", CARD_DIR)
            with open(os.path.join(library, "broken.sicard"), "w") as f:
                f.write("not a card")
            listed = list_cards(library)
            self.assertEqual([e["name"] for e in listed], ["broken.sicard", "rusty_lantern.sicard"])  # the plain zip is not a card
            self.assertIsNone(listed[0]["card"])
            self.assertEqual(listed[1]["card"].title, "The Rusty Lantern")
            self.assertTrue(listed[1]["card"].read_asset("assets/bg/stable.jpg").startswith(b"\xff\xd8"))  # assets load from the package
            self.assertRaises(CardError, import_card, os.path.join(library, "broken.sicard"), library)
            self.assertRaises(CardError, import_card, plain_zip, library)
            self.assertRaises(CardError, import_card, CARD_DIR, library)
            self.assertEqual(len(list_cards(library)), 2)
        finally:
            shutil.rmtree(work)
        self.assertEqual(list_cards(os.path.join(work, "gone")), [])

    def test_sicard_is_sealed(self):
        import shutil, tempfile, zipfile
        work = tempfile.mkdtemp()
        try:
            packed = pack_card(CARD_DIR, os.path.join(work, "a.sicard"))
            with open(packed, "rb") as f:
                blob = f.read()
            self.assertFalse(zipfile.is_zipfile(packed))
            self.assertNotIn(b"Rusty Lantern", blob)
            self.assertNotIn(b"card.json", blob)
            with open(pack_card(CARD_DIR, os.path.join(work, "b.sicard")), "rb") as f:
                self.assertNotEqual(blob, f.read())  # fresh nonce each time
            tampered = os.path.join(work, "tampered.sicard")
            with open(tampered, "wb") as f:
                f.write(blob[:200] + bytes([blob[200] ^ 1]) + blob[201:])
            self.assertIn("damaged", str(self.assertRaisesAndGet(CardError, load_card, tampered)))
            self.assertEqual(load_card(packed).data, self.card.data)
            self.assertRaises(KeyError, self.card.read_asset, "../../spec/card.schema.json")
        finally:
            shutil.rmtree(work)

    def test_card_id_must_be_file_safe(self):
        with open(os.path.join(CARD_DIR, "card.json")) as f:
            data = json.load(f)
        data["meta"]["id"] = "../evil"
        self.assertEqual(len(check_card(data)), 1)

    def test_old_save_is_fitted_to_a_changed_card(self):
        unchanged = copy.deepcopy(self.state)
        self.assertEqual(reconcile(self.card, self.state), [])
        self.assertEqual(self.state, unchanged)

        self.me["inventory"]["old_sword"] = 1
        self.me["equipment"]["weapon"] = "old_sword"
        self.me["stats"]["luck"] = 3
        del self.me["stats"]["stamina"]
        self.me["location"] = "attic"
        self.state["actors"]["ghost"] = dict(self.state["actors"]["tobin"], name="Ghost")
        del self.state["actors"]["tobin"]
        self.state["quests"]["gone_quest"] = {"status": "active", "stage": 0}
        self.state["quests"]["missing_courier"]["stage"] = 9
        self.state["shops"] = {"old_shop": {"stew": 1}, "lantern_bar": {"healing_draught": 0, "old_sword": 2}}
        del self.state["expressions"]
        self.state["generated_items"]["gen_charm"] = {"id": "gen_charm", "name": "Charm", "type": "consumable", "generated": True,
                                                      "effects": [{"stat": "luck", "amount": 1}, {"stat": "hp", "amount": 1}]}
        notes = reconcile(self.card, self.state)
        self.assertEqual(self.state["generated_items"]["gen_charm"]["effects"], [{"stat": "hp", "amount": 1}])
        self.assertEqual(len(notes), 4, notes)
        self.assertEqual(self.me["inventory"], {"healing_draught": 1, "belt_knife": 1})
        self.assertNotIn("weapon", self.me["equipment"])
        self.assertEqual(self.me["stats"], {"hp": 20, "stamina": 5, "mana": 10, "defense": 0})
        self.assertEqual(self.me["location"], "common_room")
        self.assertEqual(sorted(self.state["actors"]), ["marsh_bandit", "mira", "player", "tobin"])
        self.assertEqual(self.state["quests"], {"missing_courier": {"status": "active", "stage": 2, "met": []}})
        self.assertEqual(self.state["shops"], {"lantern_bar": {"stew": None, "healing_draught": 0}})
        self.assertEqual(self.state["expressions"], {})
        for action in [{"type": "use_item", "item": "healing_draught"}, {"type": "move", "location": "stable"}]:
            self.assertTrue(apply_action(self.card, self.state, action)["ok"])

    def test_state_pickles(self):
        self.assertEqual(pickle.loads(pickle.dumps(self.state)), self.state)

    def test_broken_references_are_reported(self):
        with open(os.path.join(CARD_DIR, "card.json")) as f:
            data = json.load(f)
        data["characters"][0]["start"]["inventory"][0]["item"] = "nope"
        data["locations"][0]["connections"].append("moon")
        data["items"][2].pop("slot")
        problems = check_card(data)
        self.assertEqual(len(problems), 4, problems)  # the slotless cloak also breaks the persona's equipment
        self.assertRaises(CardError, load_card, os.path.join(ROOT, "spec"))

    # items

    def test_use_consumable_spends_it_and_applies_effects(self):
        self.me["stats"]["hp"] = 5
        self.assertIn("Health +8, now 13", self.ok(type="use_item", item="healing_draught"))
        self.assertNotIn("healing_draught", self.me["inventory"])

    def test_using_item_not_held_is_rejected(self):
        self.assertEqual(self.rejected(type="use_item", item="stew"), "Traveler does not have Bowl of Stew.")

    def test_unknown_item_is_rejected(self):
        self.assertIn("no item", self.rejected(type="use_item", item="dragon_egg"))

    def test_items_can_be_referenced_by_name(self):
        self.ok(type="use_item", item="Healing Draught")

    def test_stat_is_clamped_to_max(self):
        self.ok(type="use_item", item="healing_draught")
        self.assertEqual(self.me["stats"]["hp"], 20)

    def test_equipment_bonus_applies_only_while_equipped(self):
        self.assertEqual(effective_stat(self.card, self.state, "player", "defense"), 1)
        self.ok(type="unequip", slot="body")
        self.assertEqual(effective_stat(self.card, self.state, "player", "defense"), 0)
        self.assertEqual(self.me["inventory"]["travel_cloak"], 1)
        self.rejected(type="unequip", slot="body")
        self.ok(type="equip", item="travel_cloak")
        self.assertNotIn("travel_cloak", self.me["inventory"])
        self.rejected(type="equip", item="travel_cloak")
        self.rejected(type="equip", item="healing_draught")

    def test_transfer_needs_same_place_and_possession(self):
        self.assertIn("not in the same place", self.rejected(type="transfer_item", item="sealed_letter", **{"from": "tobin", "to": "player"}))
        self.ok(type="move", location="stable")
        self.ok(type="transfer_item", item="sealed_letter", **{"from": "tobin", "to": "player"})
        self.assertEqual(self.me["inventory"]["sealed_letter"], 1)
        self.rejected(type="transfer_item", item="sealed_letter", **{"from": "tobin", "to": "player"})

    def test_create_item_respects_card_rule(self):
        self.ok(type="create_item", name="Odd Coin", qty=2)
        self.assertEqual(self.me["inventory"]["gen_odd_coin"], 2)
        self.ok(type="create_item", name="odd coin")
        self.assertEqual(self.me["inventory"]["gen_odd_coin"], 3)
        self.card.data["rules"]["allow_generated_items"] = False
        self.rejected(type="create_item", name="Magic Sword")
        self.ok(type="create_item", name="Bowl of Stew")  # existing card items are still fine

    def test_created_item_can_be_equipment(self):
        self.ok(type="create_item", name="Silk Dress", slot="body")
        dress = self.state["generated_items"]["gen_silk_dress"]
        self.assertEqual((dress["type"], dress["slot"]), ("equipment", "body"))
        self.assertNotIn("effects", dress)
        self.ok(type="equip", item="gen_silk_dress")
        self.assertEqual(self.me["equipment"]["body"], "gen_silk_dress")
        self.ok(type="create_item", name="Odd Hat", slot="nose")  # not a slot: a plain item
        self.assertEqual(self.state["generated_items"]["gen_odd_hat"]["type"], "misc")
        self.rejected(type="equip", item="gen_odd_hat")
        self.ok(type="create_item", name="Odd Hat", slot="head")  # said again with a slot: the one held becomes wearable
        self.assertEqual(self.me["inventory"]["gen_odd_hat"], 1)
        self.ok(type="equip", item="gen_odd_hat")
        self.card.data["rules"].setdefault("features", {})["equipment"] = False
        self.ok(type="create_item", name="Tin Ring", slot="accessory")
        self.assertEqual(self.state["generated_items"]["gen_tin_ring"]["type"], "misc")

    def test_a_state_the_story_made_up_stops_what_the_story_says(self):
        self.ok(type="set_state", state="grieving")
        self.assertNotIn("stops", self.me["states"]["grieving"])
        self.ok(type="set_state", state="Hands bound", note="rope at the wrists", stops=["items", "equipment", "attack"])
        self.assertEqual(limits(self.card, self.me), [("items", "Hands bound"), ("equipment", "Hands bound"), ("attack", "Hands bound")])
        held = apply_action(self.card, self.state, {"type": "use_item", "item": "healing_draught"}, by_player=True)
        self.assertIsNone(held["ok"])                                                               # in the way, and for the story to weigh
        self.assertIn("Traveler is hands bound (rope at the wrists) and tries to use Healing Draught all the same.", held["message"])
        self.assertEqual(self.me["inventory"]["healing_draught"], 1)
        self.assertTrue(apply_action(self.card, self.state, {"type": "move", "location": "stable"}, by_player=True)["ok"])   # not their legs
        self.ok(type="set_state", state="hands bound", note="the knots pulled tighter")              # said again without stops: it still holds
        self.assertEqual(self.me["states"]["hands_bound"]["stops"], ["items", "equipment", "attack"])
        self.ok(type="set_state", state="restrained", stops=["speech"])                             # the card's own state keeps the card's rules
        self.assertNotIn("stops", self.me["states"]["restrained"])
        self.assertEqual(limits(self.card, self.me), [("all", "Restrained")])
        self.ok(type="clear_state", state="restrained")
        self.ok(type="clear_state", state="hands bound")
        self.assertTrue(apply_action(self.card, self.state, {"type": "use_item", "item": "healing_draught"}, by_player=True)["ok"])
        self.ok(type="set_state", state="asleep")
        self.assertEqual([what for what, name in limits(self.card, self.me)], ["all", "speech", "sight", "hearing"])

    def test_what_the_player_does_with_something_the_game_does_not_have_is_left_to_the_story(self):
        """The helper that reads the player's message can only answer in game actions. When what
        it names is not there to act on, the engine rules on nothing and the story decides."""
        before = copy.deepcopy(self.state)
        for attempt in ({"type": "use_item", "item": "stew"},                      # a real item that is not theirs
                        {"type": "use_item", "item": "cup of wine"},               # no such item
                        {"type": "equip", "item": "a coat from the chair"},
                        {"type": "transfer_item", "item": "stew", "to": "mira"},
                        {"type": "transfer_item", "item": "healing_draught", "to": "the stranger"},   # no such person
                        {"type": "move", "location": "the kitchen"},               # no such place
                        {"type": "use_skill", "skill": "time stop"},               # no such skill
                        {"type": "start_battle", "enemies": ["a passing thug"]},
                        {"type": "buy", "shop": "the pedlar", "item": "stew"}):
            left = apply_action(self.card, self.state, attempt, by_player=True)
            self.assertIsNone(left["ok"], "%s: %s" % (attempt, left["message"]))
            self.assertIn("nothing the game tracks was used or changed", left["message"])
            self.assertFalse(apply_action(self.card, self.state, attempt)["ok"], attempt)          # the narrator's own slip is still refused
        self.assertEqual(self.state, before)
        self.assertTrue(apply_action(self.card, self.state, {"type": "use_item", "item": "healing_draught"}, by_player=True)["ok"])
        # What the game does have, it still rules on: too little money, more than they hold.
        self.me["money"] = 0
        self.assertIs(apply_action(self.card, self.state, {"type": "buy", "shop": "lantern_bar", "item": "stew"}, by_player=True)["ok"], False)
        self.assertIs(apply_action(self.card, self.state, {"type": "transfer_item", "item": "belt_knife", "qty": 5, "to": "mira"}, by_player=True)["ok"], False)

    def test_what_is_worn_can_fall_off_be_lost_or_handed_over(self):
        self.ok(type="unequip", item="Travel Cloak")                              # named by the item, not the slot
        self.assertEqual((self.me["equipment"].get("body"), self.me["inventory"]["travel_cloak"]), (None, 1))
        self.rejected(type="unequip", item="travel_cloak")
        self.ok(type="equip", item="travel_cloak")
        self.ok(type="remove_item", item="travel_cloak")                          # lost while worn
        self.assertEqual((self.me["equipment"].get("body"), self.me["inventory"].get("travel_cloak")), (None, None))
        self.ok(type="add_item", item="travel_cloak")
        self.ok(type="equip", item="travel_cloak")
        self.rejected(type="remove_item", item="travel_cloak", qty=3)             # still more than they have, and nothing comes off
        self.assertEqual(self.me["equipment"]["body"], "travel_cloak")
        self.ok(type="transfer_item", item="travel_cloak", to="mira", **{"from": "player"})
        self.assertEqual((self.me["equipment"].get("body"), self.state["actors"]["mira"]["inventory"]["travel_cloak"]), (None, 1))
        self.ok(type="equip", who="mira", item="travel_cloak")                    # the story dresses someone
        self.assertEqual(self.state["actors"]["mira"]["equipment"]["body"], "travel_cloak")

    def test_a_plain_item_the_story_made_can_be_put_on(self):
        self.ok(type="create_item", name="Wrap Dress")
        before = copy.deepcopy(self.state)
        left = apply_action(self.card, self.state, {"type": "equip", "item": "gen_wrap_dress"}, by_player=True)
        self.assertIsNone(left["ok"])                                             # no slot named: left to the story, not refused
        self.assertIn("change_item", left["message"])
        self.assertEqual(self.state, before)
        worn = apply_action(self.card, self.state, {"type": "equip", "item": "gen_wrap_dress", "slot": "body"}, by_player=True)
        self.assertTrue(worn["ok"], worn["message"])
        self.assertEqual(self.me["equipment"]["body"], "gen_wrap_dress")
        self.assertEqual(self.state["generated_items"]["gen_wrap_dress"]["slot"], "body")
        self.ok(type="equip", item="travel_cloak", slot="head")                   # a slot never moves real equipment
        self.assertEqual(self.me["equipment"]["body"], "travel_cloak")
        self.ok(type="create_item", name="Red Vial", effects=[{"stat": "hp", "amount": 3}])
        self.rejected(type="equip", item="gen_red_vial", slot="accessory")        # only plain things: what a potion does is never worn
        self.rejected(type="equip", item="healing_draught", slot="body")           # nor the card's own

    def test_created_item_can_do_something(self):
        hp = self.card.stats["hp"]
        self.ok(type="change_stat", stat="hp", amount=-5)
        hurt = self.me["stats"]["hp"]
        self.ok(type="create_item", name="Red Vial", effects=[{"stat": "hp", "amount": 3}, {"stat": "luck_of_the_draw", "amount": 9}, {"stat": "hp", "amount": 0}])
        vial = self.state["generated_items"]["gen_red_vial"]
        self.assertEqual((vial["type"], vial["effects"]), ("consumable", [{"stat": "hp", "amount": 3}]))  # unknown stats and empty amounts are dropped
        self.ok(type="use_item", item="gen_red_vial")
        self.assertEqual(self.me["stats"]["hp"], hurt + 3)
        self.assertNotIn("gen_red_vial", self.me["inventory"])
        self.ok(type="create_item", name="Giant's Belt", slot="accessory", effects=[{"stat": "hp", "amount": 100000}])
        limit = (hp["max"] - hp.get("min", 0)) / 2.0 if hp.get("max") is not None else abs(hp["default"])
        self.assertEqual(self.state["generated_items"]["gen_giant_s_belt"]["effects"], [{"stat": "hp", "amount": limit}])

    def test_the_story_can_change_what_it_made_and_nothing_else(self):
        self.ok(type="create_item", name="Grey Rag")
        rag = self.state["generated_items"]["gen_grey_rag"]
        self.ok(type="change_item", item="gen_grey_rag", slot="head", name="Grey Hood")
        self.assertEqual((rag["type"], rag["slot"], rag["name"]), ("equipment", "head", "Grey Hood"))
        self.ok(type="equip", item="Grey Hood")
        self.ok(type="change_item", item="gen_grey_rag", slot="accessory")       # worn on the head, now a scarf: back in the pack
        self.assertEqual((self.me["equipment"].get("head"), self.me["inventory"]["gen_grey_rag"]), (None, 1))
        self.ok(type="equip", item="gen_grey_rag")
        self.ok(type="change_item", item="gen_grey_rag", kind="consumable", effects=[{"stat": "hp", "amount": 1}])
        self.assertEqual((rag["type"], "slot" in rag, self.me["equipment"].get("accessory"), self.me["inventory"]["gen_grey_rag"]), ("consumable", False, None, 1))
        self.ok(type="change_item", item="gen_grey_rag", kind="misc")
        self.assertEqual((rag["type"], "effects" in rag), ("misc", False))
        self.rejected(type="change_item", item="gen_grey_rag", kind="misc")       # nothing to change
        self.rejected(type="change_item", item="gen_grey_rag", kind="equipment")  # no slot
        self.rejected(type="change_item", item="gen_grey_rag", name="Travel Cloak")
        self.rejected(type="change_item", item="travel_cloak", kind="misc")       # the card's own
        self.ok(type="create_item", name="Grey Hood", kind="consumable")          # said again as something else: changed, not doubled
        self.assertEqual((rag["type"], self.me["inventory"]["gen_grey_rag"]), ("consumable", 1))
        self.ok(type="create_item", name="Grey Hood", kind="consumable")          # said again as it is: one more
        self.assertEqual(self.me["inventory"]["gen_grey_rag"], 2)

    # shops and money

    def test_buy_checks_money_stock_and_location(self):
        self.ok(type="buy", shop="lantern_bar", item="stew", qty=2)
        self.assertEqual((self.me["money"], self.me["inventory"]["stew"]), (4, 2))
        self.assertEqual(self.state["actors"]["mira"]["money"], 42)
        self.assertIn("costs 8 Gold", self.rejected(type="buy", shop="lantern_bar", item="healing_draught"))
        self.assertIn("does not sell", self.rejected(type="buy", shop="lantern_bar", item="belt_knife"))
        self.me["money"] = 100
        self.assertIn("only has 2", self.rejected(type="buy", shop="lantern_bar", item="healing_draught", qty=3))
        self.ok(type="buy", shop="lantern_bar", item="healing_draught", qty=2)
        self.assertIn("sold out", self.rejected(type="buy", shop="lantern_bar", item="healing_draught"))
        self.ok(type="move", location="stable")
        self.assertIn("is not at", self.rejected(type="buy", shop="lantern_bar", item="stew"))

    def test_sell_pays_half_and_refuses_worthless_items(self):
        self.assertEqual(sell_price(self.card, self.state, "lantern_bar", "healing_draught"), 4)
        self.ok(type="sell", shop="lantern_bar", item="healing_draught")
        self.assertEqual(self.me["money"], 10)
        self.assertEqual(self.state["shops"]["lantern_bar"]["healing_draught"], 3)
        self.rejected(type="sell", shop="lantern_bar", item="healing_draught")
        self.ok(type="add_item", item="cellar_key")
        self.assertIn("will not buy", self.rejected(type="sell", shop="lantern_bar", item="cellar_key"))

    def test_money_cannot_go_negative(self):
        self.rejected(type="change_money", amount=-7)
        self.ok(type="change_money", amount=-6)
        self.assertEqual(self.me["money"], 0)

    # movement and quests

    def test_the_player_walks_only_to_neighbouring_places(self):
        by_choice = lambda **action: apply_action(self.card, self.state, action, by_player=True)
        self.assertTrue(by_choice(type="move", location="stable")["ok"])
        before = copy.deepcopy(self.state)
        far = by_choice(type="move", location="cellar")                          # the cellar is not next to the stable
        self.assertIsNone(far["ok"])                                             # neither done nor refused
        self.assertIn("for the story to decide", far["message"])
        self.assertEqual(self.state, before)
        self.assertFalse(by_choice(type="move", location="stable")["ok"])        # already there
        self.assertTrue(by_choice(type="move", location="common_room")["ok"])

    def test_the_story_can_take_anyone_anywhere(self):
        self.ok(type="move", location="stable")
        self.assertEqual(self.ok(type="move", location="cellar"), "Traveler leaves Stable and goes to Cellar.")     # a portal, say
        self.ok(type="move", who="tobin", location="cellar")
        self.ok(type="move", who="marsh_bandit", location="cellar")              # someone who was off the map
        self.assertEqual(set(a["location"] for w, a in self.state["actors"].items() if w != "mira"), {"cellar"})
        self.rejected(type="move", who="tobin", location="cellar")               # already there
        self.rejected(type="move", location="atlantis")                          # still has to be a real place

    def test_a_reward_handed_over_in_the_story_is_the_one_already_paid(self):
        """Seen in play: the quest paid two stars when it finished, and a turn later the story had
        the reward presented, which the bookkeeper recorded as two more."""
        self.test_quest_advances_then_completes_with_rewards()
        gold, draughts = self.me["money"], self.me["inventory"].get("healing_draught", 0)
        self.state["turn"] += 1
        results = apply_actions(self.card, self.state, [{"type": "change_money", "who": "player", "amount": 15},
                                                        {"type": "add_item", "who": "player", "item": "healing_draught", "qty": 1}])
        self.assertEqual([r["message"] for r in results], ["Already given as the reward for The Missing Courier."] * 2)
        self.assertEqual((self.me["money"], self.me["inventory"].get("healing_draught", 0)), (gold, draughts))
        apply_actions(self.card, self.state, [{"type": "change_money", "who": "player", "amount": 15}])
        self.assertEqual(self.me["money"], gold + 15)                  # it is only taken off once: this is new money

    def test_the_most_someone_can_have_of_a_stat_can_grow_and_shrink(self):
        self.do(type="change_stat", stat="hp", amount=-5)
        self.assertIn("20/25", self.ok(type="change_stat_max", stat="hp", amount=5))        # grows, and what they have grows with it
        self.do(type="change_stat", stat="hp", amount=100)
        self.assertEqual(self.me["stats"]["hp"], 25)
        self.assertIn("15/15", self.ok(type="change_stat_max", stat="hp", amount=-10))      # shrinks, and nothing stays above it
        self.assertEqual(self.state["actors"]["mira"].get("max", {}).get("hp"), None)       # one person's growth is theirs alone
        self.ok(type="change_stat_max", who="mira", stat="hp", amount=3)
        self.rejected(type="change_stat_max", stat="hp", amount=0)
        self.rejected(type="change_stat_max", stat="hp", amount="lots")
        self.rejected(type="change_stat_max", stat="luck", amount=1)
        self.ok(type="change_stat_max", stat="hp", amount=-1000)
        self.assertEqual((self.me["stats"]["hp"], self.me["max"]["hp"]), (0, 0))            # never below the stat's floor

    def test_a_character_can_have_their_own_maximum(self):
        """Asked for after play: with one Mana limit for everyone, nobody could tell that 900 was all one character could hold."""
        data = copy.deepcopy(self.card.data)
        mira = next(c for c in data["characters"] if c["id"] == "mira")
        mira.setdefault("start", {})["max"] = {"hp": 35, "mana": 4}
        mira["start"].setdefault("stats", {})["hp"] = 30
        card = Card(data, CARD_DIR)
        self.assertEqual(check_card(data), [])
        state = new_game(card)
        her = state["actors"]["mira"]
        self.assertEqual((her["stats"]["hp"], her["stats"]["mana"], her["max"]), (30, 4, {"hp": 35, "mana": 4}))     # no starting Mana given: she starts full
        apply_actions(card, state, [{"type": "change_stat", "who": "mira", "stat": "hp", "amount": 100}, {"type": "change_stat", "who": "player", "stat": "hp", "amount": 100}])
        self.assertEqual((her["stats"]["hp"], state["actors"]["player"]["stats"]["hp"]), (35, 20))                    # each up to their own
        self.assertIn("Health (hp) 35/35, Stamina (stamina) 10/10, Mana (mana) 4/4", prompt.describe_state(card, state))
        old = new_game(self.card)                                                                                      # a game begun before the card gave her one
        old["actors"]["mira"]["stats"]["mana"] = 9
        reconcile(card, old)
        self.assertEqual((old["actors"]["mira"]["max"], old["actors"]["mira"]["stats"]["mana"]), ({"hp": 35, "mana": 4}, 4))
        mira["start"]["min"] = {"hp": 5}                                                                              # and their own minimum
        card = Card(data, CARD_DIR)
        self.assertEqual(check_card(data), [])
        state = new_game(card)
        apply_actions(card, state, [{"type": "change_stat", "who": "mira", "stat": "hp", "amount": -100}, {"type": "change_stat_max", "who": "mira", "stat": "hp", "amount": -100}])
        self.assertEqual((state["actors"]["mira"]["stats"]["hp"], state["actors"]["mira"]["max"]["hp"]), (5, 5))
        reconcile(card, old)
        self.assertEqual(old["actors"]["mira"]["min"], {"hp": 5})
        mira["start"]["stats"]["hp"] = 50
        mira["start"]["max"]["luck"] = 3
        mira["start"]["min"] = {"hp": 40, "mana": 9, "luck": 1}
        self.assertEqual(len(check_card(data)), 5)

    def test_a_new_place_can_be_gone_to_in_the_same_breath(self):
        """Seen in play: "There is no location called 'royal_gardens'", then "A new place: Royal Gardens"."""
        told = apply_actions(self.card, self.state, [{"type": "move", "who": "player", "location": "royal_gardens"},
                                                     {"type": "move", "who": "mira", "location": "Royal gardens"},
                                                     {"type": "create_location", "name": "Royal Gardens", "description": "Clipped hedges."}])
        self.assertEqual([r["ok"] for r in told], [True, True, True])
        self.assertTrue(told[0]["message"].startswith("A new place: Royal Gardens"))
        self.assertEqual((self.me["location"], self.state["actors"]["mira"]["location"]), ("gen_royal_gardens", "gen_royal_gardens"))
        self.ok(type="add_item", item="Healing-Draught")                                    # the same leniency for anything named
        self.rejected(type="move", location="royal")                                        # but not a guess at half a name

    def test_only_the_very_reward_is_held_back(self):
        """Seen with real models: a merchant's gift of 40 gold, soon after a 15 gold reward, must arrive whole."""
        self.test_quest_advances_then_completes_with_rewards()
        gold = self.me["money"]
        apply_actions(self.card, self.state, [{"type": "change_money", "who": "player", "amount": 40}, {"type": "change_money", "who": "mira", "amount": 15}])
        self.assertEqual(self.me["money"], gold + 40)
        self.state["turn"] += 20                                       # long after, the same amount is simply new money
        apply_actions(self.card, self.state, [{"type": "change_money", "who": "player", "amount": 15}])
        self.assertEqual(self.me["money"], gold + 55)
        state = new_game(self.card)
        me = state["actors"]["player"]
        apply_actions(self.card, state, [{"type": "change_money", "who": "player", "amount": 15}])
        self.assertEqual(me["money"], new_game(self.card)["actors"]["player"]["money"] + 15)    # no quest was paid: nothing is held back

    def test_quest_advances_then_completes_with_rewards(self):
        self.rejected(type="quest_start", quest="missing_courier")
        self.assertIn("Find what the courier", self.ok(type="quest_advance", quest="missing_courier"))
        # The second objective is the game's to mark: no model can move it, holding the letter does.
        self.assertIn("The game itself marks", self.rejected(type="quest_advance", quest="missing_courier"))
        told = apply_actions(self.card, self.state, [{"type": "move", "location": "stable"}, {"type": "transfer_item", "item": "sealed_letter", "from": "tobin", "to": "player"}])
        self.assertIn("new objective: Decide what to do with the letter.", told[-1]["message"])
        self.assertIn("Reward: 15 Gold, Healing Draught", self.ok(type="quest_advance", quest="missing_courier"))
        self.assertEqual((self.me["money"], self.me["inventory"]["healing_draught"]), (21, 2))
        self.rejected(type="quest_advance", quest="missing_courier")
        self.rejected(type="quest_fail", quest="missing_courier")

    def test_getting_there_early_carries_the_quest_past_what_came_before(self):
        quest = self.state["quests"]["missing_courier"]
        told = apply_actions(self.card, self.state, [{"type": "move", "location": "stable"}, {"type": "transfer_item", "item": "sealed_letter", "from": "tobin", "to": "player"}])
        self.assertEqual(quest["stage"], 2)                       # never learned of the horse, but holds the letter
        self.assertEqual([r["message"] for r in told][-1], "Quest The Missing Courier: new objective: Decide what to do with the letter.")
        self.assertEqual(apply_actions(self.card, self.state, []), [])           # and it is only said once

    def test_a_condition_true_from_the_start_skips_nothing(self):
        stages = self.card.quests["missing_courier"]["stages"]
        stages[2]["done_if"] = [{"type": "at", "location": "common_room"}]       # "bring it back to the inn"
        state = new_game(self.card)
        quest = state["quests"]["missing_courier"]
        self.assertEqual(apply_actions(self.card, state, []), [])                # already at the inn: that proves nothing yet
        self.assertEqual(quest["stage"], 0)
        apply_actions(self.card, state, [{"type": "move", "location": "stable"}], by_player=True)
        apply_actions(self.card, state, [{"type": "transfer_item", "item": "sealed_letter", "from": "tobin", "to": "player"}])
        self.assertEqual(quest["stage"], 2)
        done = apply_actions(self.card, state, [{"type": "move", "location": "common_room"}], by_player=True)
        self.assertIn("Quest completed: The Missing Courier.", done[-1]["message"])
        self.assertEqual(quest["status"], "done")

    def test_other_conditions_and_bad_ones(self):
        from aigame.state import condition_met
        self.state["actors"]["tobin"]["relationship"] = 50
        for condition, met in [({"type": "relationship", "who": "tobin", "at_least": 50}, True), ({"type": "relationship", "who": "tobin", "at_least": 51}, False),
                               ({"type": "level", "at_least": 1}, True), ({"type": "level", "at_least": 2}, False),
                               ({"type": "at", "who": "tobin", "location": "stable"}, True), ({"type": "has_item", "who": "mira", "item": "cellar_key"}, True),
                               ({"type": "has_item", "who": "nobody", "item": "stew"}, False), ({"type": "wish"}, False)]:
            self.assertEqual(bool(condition_met(self.card, self.state, condition)), met, condition)
        card = json.loads(json.dumps(self.card.data))
        card["quests"][0]["stages"][0]["done_if"] = [{"type": "has_item", "item": "crown"}, {"type": "at", "location": "moon"}, {"type": "relationship", "at_least": 5},
                                                     {"type": "level"}, {"type": "wish"}, {"type": "at", "who": "ghost", "location": "stable"}]
        problems = "\n".join(check_card(card))
        for expected in ("unknown item 'crown'", "unknown location 'moon'", "must name a character", "a level condition needs a number", "unknown type 'wish'", "unknown character 'ghost'"):
            self.assertIn(expected, problems)

    # malformed input from an LLM

    def test_a_state_said_to_be_over_is_taken_as_cleared(self):
        self.ok(type="set_state", who="tobin", state="Charging at Sekke", note="mid-match")
        self.assertEqual(self.state["actors"]["tobin"]["states"]["charging_at_sekke"]["note"], "mid-match")
        # What a model did in play: "updated" the state to say it had ended, instead of clearing it.
        self.assertEqual(self.ok(type="set_state", who="tobin", state="Charging at Sekke", note="no longer charging; match over"), "Tobin is no longer charging at sekke.")
        self.assertEqual(self.state["actors"]["tobin"]["states"], {})
        # Cleared means gone: nothing of it is left on the character, and clearing it again finds nothing to clear.
        self.assertIn("is not", self.rejected(type="clear_state", who="tobin", state="charging at sekke"))
        self.ok(type="set_state", who="tobin", state="asleep", note="knocked over by the mare")          # "over" inside an ordinary note ends nothing
        self.assertIn("asleep", self.state["actors"]["tobin"]["states"])
        self.ok(type="set_state", who="mira", state="away", note="no longer at the inn")                # a new state is set whatever its note says
        self.assertIn("away", self.state["actors"]["mira"]["states"])

    def test_states_stack_and_each_ends_on_its_own(self):
        mira = self.state["actors"]["mira"]
        self.ok(type="set_state", who="mira", state="restrained", note="tied to a chair")
        self.ok(type="set_state", who="mira", state="gagged", note="a rag in her mouth")
        self.ok(type="set_state", who="mira", state="blindfolded")
        self.assertEqual(sorted(mira["states"]), ["blindfolded", "gagged", "restrained"])
        self.assertEqual(describe_states(mira), "Blindfolded, Gagged (a rag in her mouth), Restrained (tied to a chair)")
        # Setting one she already has replaces its note; it does not add a second copy.
        self.ok(type="set_state", who="mira", state="restrained", note="tied to a chair, no longer gagged")       # says the gag ended, not the ropes
        self.assertEqual((len(mira["states"]), mira["states"]["restrained"]["note"]), (3, "tied to a chair, no longer gagged"))
        self.assertEqual(self.ok(type="clear_state", who="mira", state="gagged"), "Mira Oakhand is no longer gagged.")
        self.assertEqual(sorted(mira["states"]), ["blindfolded", "restrained"])                                    # the other two hold
        self.ok(type="set_state", who="mira", state="gagged", note="gagged again")                                  # and a cleared state can begin again later
        self.ok(type="set_state", who="mira", state="blindfolded", note="it is over")
        self.assertEqual(sorted(mira["states"]), ["gagged", "restrained"])

    def test_malformed_actions_are_rejected_not_crashing(self):
        for action in ["eat stew", {}, {"type": "fly"}, {"type": "use_item"}, {"type": "use_item", "item": 5},
                       {"type": "add_item", "item": "stew", "qty": 0}, {"type": "add_item", "item": "stew", "qty": "many"},
                       {"type": "change_stat", "stat": "hp", "amount": "lots"}, {"type": "change_stat", "stat": "luck", "amount": 1},
                       {"type": "use_item", "item": "healing_draught", "who": "nobody"}, {"type": "unequip", "slot": ["body"]}]:
            self.assertFalse(apply_action(self.card, self.state, action)["ok"], action)


class OptionalSystemsTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(CARD_DIR)
        self.state = new_game(self.card)
        self.me = self.state["actors"]["player"]

    def do(self, card=None, **action):
        return apply_action(card or self.card, self.state, action)

    def variant(self, **features):
        """The sample card with some systems switched."""
        data = copy.deepcopy(self.card.data)
        data["rules"]["features"] = dict(data["rules"].get("features", {}), **features)
        self.assertEqual(check_card(data), [])
        return Card(data, CARD_DIR)

    def test_skill_spends_cost_and_hits_target(self):
        result = self.do(type="use_skill", skill="quick_strike", target="mira")
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.me["stats"]["stamina"], self.state["actors"]["mira"]["stats"]["hp"]), (3, 16))
        self.do(type="change_stat", stat="hp", amount=-10)
        self.assertTrue(self.do(type="use_skill", skill="Field Dressing")["ok"])        # self target, by name
        self.assertEqual((self.me["stats"]["stamina"], self.me["stats"]["hp"]), (0, 15))
        self.assertIn("not have enough Stamina", self.do(type="use_skill", skill="quick_strike", target="mira")["message"])
        self.assertIn("needs a target", self.do(type="use_skill", skill="quick_strike")["message"])
        self.assertIn("not in the same place", self.do(type="use_skill", skill="quick_strike", target="tobin")["message"])
        self.assertIn("does not know", self.do(type="use_skill", who="tobin", skill="quick_strike", target="tobin")["message"])

    def test_locked_skill_unlocks_by_level_or_story(self):
        self.assertIn("not unlocked", self.do(type="use_skill", skill="marsh_light", target="mira")["message"])
        message = self.do(type="gain_xp", amount=50)["message"]
        self.assertIn("reaches level 2 (Health +5, Mana +2)", message)
        self.assertIn("learns Marsh Light", message)
        self.assertEqual((self.me["level"], self.me["xp"], self.me["max"], self.me["stats"]["mana"]), (2, 0, {"hp": 25, "mana": 12}, 12))
        self.assertEqual(effective_stat(self.card, self.state, "player", "hp"), 25)
        self.assertTrue(self.do(type="use_skill", skill="marsh_light", target="mira")["ok"])
        self.assertTrue(self.do(type="unlock_skill", who="tobin", skill="field_dressing")["ok"])
        self.assertFalse(self.do(type="unlock_skill", who="tobin", skill="field_dressing")["ok"])

    def test_levels_need_more_experience_each_time(self):
        self.do(type="gain_xp", amount=160)        # 50 for level 2, 100 for level 3, 10 left over
        self.assertEqual((self.me["level"], self.me["xp"]), (3, 10))
        self.assertFalse(self.do(type="gain_xp", amount=0)["ok"])

    def test_relationship_stays_in_range(self):
        self.assertEqual(self.do(type="change_relationship", who="mira", amount=5)["message"], "Mira Oakhand's trust +5, now 25/100.")
        self.do(type="change_relationship", who="mira", amount=500)
        self.assertEqual(self.state["actors"]["mira"]["relationship"], 100)
        self.assertFalse(self.do(type="change_relationship", who="player", amount=5)["ok"])

    def test_switched_off_systems_reject_their_actions(self):
        bare = self.variant(inventory=False, money=False, levels=False, skills=False, relationships=False)
        before = copy.deepcopy(self.state)
        for action in [dict(type="use_item", item="healing_draught"), dict(type="equip", item="belt_knife"), dict(type="unequip", slot="body"),
                       dict(type="buy", shop="lantern_bar", item="stew"), dict(type="change_money", amount=5), dict(type="gain_xp", amount=50),
                       dict(type="use_skill", skill="field_dressing"), dict(type="change_relationship", who="mira", amount=5)]:
            result = self.do(bare, **action)
            self.assertIn("does not use", result["message"], action)
        self.assertEqual(self.state, before)
        self.assertTrue(self.do(bare, type="move", location="stable")["ok"])
        self.assertFalse(self.do(self.variant(inventory=False), type="buy", shop="lantern_bar", item="stew")["ok"])
        self.do(bare, type="quest_advance", quest="missing_courier"); self.do(bare, type="quest_advance", quest="missing_courier")
        self.assertEqual(self.do(bare, type="quest_advance", quest="missing_courier")["message"], "Quest completed: The Missing Courier.")

    def test_casual_card_tracks_only_relationships(self):
        cafe = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        state = new_game(cafe)
        self.assertEqual(state["actors"]["player"]["stats"], {})
        self.assertEqual(state["actors"]["noor"]["relationship"], 30)
        self.assertTrue(apply_action(cafe, state, {"type": "change_relationship", "who": "noor", "amount": 3})["ok"])
        self.assertFalse(apply_action(cafe, state, {"type": "change_money", "amount": 3})["ok"])

    def test_bad_optional_rules_are_reported(self):
        data = copy.deepcopy(self.card.data)
        data["rules"]["features"]["flying"] = True
        data["rules"]["leveling"]["gains"]["luck"] = 1
        data["skills"][0]["cost"] = {"rage": 2}
        data["default_persona"]["start"]["skills"].append({"skill": "teleport"})
        self.assertEqual(len(check_card(data)), 4)

    def test_old_save_gains_the_new_fields(self):
        for actor in self.state["actors"].values():
            for key in ("level", "xp", "max", "skills", "relationship"):
                del actor[key]
        self.assertEqual(reconcile(self.card, self.state), [])
        self.assertEqual(self.me["level"], 1)
        self.assertIn("marsh_light", self.me["skills"])
        self.assertEqual(self.state["actors"]["mira"]["relationship"], 20)


class Dice(object):
    """A stand-in for the random generator: random() gives the queued numbers in turn."""
    def __init__(self, *rolls):
        self.rolls = list(rolls)

    def random(self):
        return self.rolls.pop(0) if self.rolls else 0.99

    def choice(self, options):
        return options[0]


class BattleTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(CARD_DIR)
        self.state = new_game(self.card)
        self.me = self.state["actors"]["player"]
        self.bandit = self.state["actors"]["marsh_bandit"]

    def start(self, *enemies):
        result = apply_action(self.card, self.state, {"type": "start_battle", "enemies": list(enemies or ["marsh_bandit"])})
        self.assertTrue(result["ok"], result)

    def turn(self, *rolls, **action):
        return battle.take_turn(self.card, self.state, action, Dice(*rolls))

    def test_starting_a_fight(self):
        self.assertIsNone(self.bandit["location"])
        self.assertFalse(apply_action(self.card, self.state, {"type": "start_battle", "enemies": ["tobin"]})["ok"])   # he is in the stable
        self.assertFalse(apply_action(self.card, self.state, {"type": "start_battle", "enemies": ["player"]})["ok"])
        self.assertFalse(apply_action(self.card, self.state, {"type": "start_battle", "enemies": []})["ok"])
        self.start("Marsh Bandit", "mira")
        self.assertEqual(self.state["battle"], {"enemies": ["marsh_bandit", "mira"], "round": 1})
        self.assertEqual(self.bandit["location"], "common_room")                                                      # walked into the scene
        for action in [{"type": "start_battle", "enemies": ["mira"]}, {"type": "move", "location": "stable"}, {"type": "buy", "shop": "lantern_bar", "item": "stew"}]:
            self.assertIn("middle of a fight", apply_action(self.card, self.state, action)["message"])

    def test_a_round_is_player_then_enemies(self):
        self.start()
        results = self.turn(type="battle_attack", target="marsh_bandit")
        # basic damage 3, bandit has no defense; the player's cloak gives defense 1, so the bandit does 2
        self.assertEqual([r["message"] for r in results], ["Traveler attacks Marsh Bandit (Health -3, now 9).", "Marsh Bandit attacks Traveler (Health -2, now 18)."])
        self.assertEqual(self.state["battle"]["round"], 2)
        results = self.turn(0.1, type="battle_attack", target="marsh_bandit")                                          # a low roll makes him use a skill
        self.assertIn("Marsh Bandit uses Quick Strike on Traveler", results[1]["message"])
        self.assertEqual(self.bandit["stats"]["stamina"], 8)

    def test_rejected_choice_costs_no_turn(self):
        self.start()
        self.assertEqual(len(self.turn(type="battle_attack", target="mira")), 1)
        self.assertEqual(len(self.turn(type="use_skill", skill="marsh_light", target="marsh_bandit")), 1)             # still locked
        self.assertEqual(len(self.turn(type="dance")), 1)
        self.assertEqual((self.me["stats"]["hp"], self.state["battle"]["round"]), (20, 1))

    def test_skills_and_items_work_in_a_fight(self):
        self.start()
        self.me["stats"]["hp"] = 10
        results = self.turn(type="use_item", item="healing_draught")
        self.assertIn("Health +8, now 18", results[0]["message"])
        results = self.turn(type="use_skill", skill="quick_strike", target="marsh_bandit")
        self.assertEqual(self.bandit["stats"]["hp"], 8)

    def test_victory_gives_experience_and_ends_the_fight(self):
        self.start()
        self.bandit["stats"]["hp"] = 3
        results = self.turn(type="battle_attack", target="marsh_bandit")
        self.assertEqual([r["message"] for r in results][1:], ["The fight is won. Defeated: Marsh Bandit.", "Traveler gains 30 experience."])
        self.assertIsNone(self.state["battle"])
        self.assertEqual((self.me["xp"], self.me["stats"]["hp"]), (30, 20))                                            # no enemy turn after the win

    def test_defeat_follows_the_card(self):
        self.start()
        self.me["stats"]["hp"] = 2
        self.me["location"] = "stable"
        self.bandit["location"] = "stable"
        results = self.turn(type="battle_attack", target="marsh_bandit")
        self.assertEqual(results[-1]["message"], "Traveler is beaten and left for dead, and comes round later at Common Room with 1 Health.")
        self.assertEqual((self.me["stats"]["hp"], self.me["location"], self.state["battle"], self.state["game_over"]), (1, "common_room", None, False))

        self.card.data["rules"]["battle"]["on_defeat"] = {"type": "game_over"}
        self.start("mira")
        self.turn(type="battle_attack", target="mira")
        self.assertTrue(self.state["game_over"])

    def test_fleeing_is_a_coin_toss(self):
        self.start()
        failed = self.turn(0.9, type="battle_flee")
        self.assertEqual((failed[0]["ok"], len(failed), self.state["battle"]["round"]), (False, 2, 2))
        escaped = self.turn(0.1, type="battle_flee")
        self.assertEqual((escaped[0]["ok"], len(escaped), self.state["battle"]), (True, 1, None))

    def test_story_told_fights_and_bad_battle_rules(self):
        data = copy.deepcopy(self.card.data)
        data["rules"]["battle"] = {"mode": "free"}
        self.assertEqual(check_card(data), [])
        result = apply_action(Card(data, CARD_DIR), self.state, {"type": "start_battle", "enemies": ["mira"]})
        self.assertIn("told in the story", result["message"])
        data["rules"]["battle"] = {"mode": "system", "health_stat": "luck", "attack_stat": "rage", "on_defeat": {"type": "survive", "location": "moon"}}
        self.assertEqual(len(check_card(data)), 3)


class StatesTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(CARD_DIR)
        self.state = new_game(self.card)
        self.me = self.state["actors"]["player"]
        self.mira = self.state["actors"]["mira"]

    def world(self, **action):
        """Something the narrator says happened."""
        return apply_action(self.card, self.state, action)

    def player(self, **action):
        """Something the player chose to do."""
        return apply_action(self.card, self.state, action, by_player=True)

    def test_a_restrained_player_is_held_to_it(self):
        self.assertEqual(self.world(type="set_state", state="restrained", note="tied to a chair by Mira")["message"], "Traveler is now restrained (tied to a chair by Mira).")
        for action in [dict(type="move", location="stable"), dict(type="use_item", item="healing_draught"), dict(type="unequip", slot="body"),
                       dict(type="use_skill", skill="field_dressing"), dict(type="buy", shop="lantern_bar", item="stew"),
                       dict(type="transfer_item", item="belt_knife", **{"from": "player", "to": "mira"}), dict(type="start_battle", enemies=["mira"])]:
            tried = self.player(**action)
            self.assertIsNone(tried["ok"], action)                                              # neither done nor refused: the story weighs it
            self.assertIn("Traveler is restrained (tied to a chair by Mira) and tries to ", tried["message"])
            self.assertIn("Whether this gets them past what holds them is yours to decide", tried["message"])
        self.assertIn("tries to go to Stable all the same", self.player(type="move", location="stable")["message"])
        self.assertEqual((self.me["location"], self.me["inventory"]["healing_draught"], "restrained" in self.me["states"]), ("common_room", 1, True))   # nothing was done
        # Someone a state has taken out of things altogether cannot so much as try.
        self.world(type="set_state", state="unconscious")
        self.assertEqual(self.player(type="move", location="stable"), {"action": {"type": "move", "location": "stable"}, "ok": False, "message": "Traveler cannot do that while restrained."})
        self.world(type="clear_state", state="unconscious")
        # the story can still move them, and free them
        self.assertTrue(self.world(type="move", location="cellar")["ok"])
        self.assertEqual(self.world(type="clear_state", state="Restrained")["message"], "Traveler is no longer restrained.")
        self.assertTrue(self.player(type="move", location="common_room")["ok"])
        self.assertFalse(self.world(type="clear_state", state="restrained")["ok"])

    def test_states_can_be_custom_or_made_up(self):
        self.card.data["states"] = [{"id": "hobbled", "name": "Hobbled", "blocks": ["move"]}]
        self.card.states["hobbled"] = self.card.data["states"][0]
        self.world(type="set_state", state="hobbled")
        self.assertIsNone(self.player(type="move", location="stable")["ok"])
        self.assertTrue(self.player(type="use_item", item="healing_draught")["ok"])           # only movement is in the way
        self.assertEqual(self.world(type="set_state", who="mira", state="Slightly Drunk")["message"], "Mira Oakhand is now slightly drunk.")
        self.assertEqual(self.mira["states"], {"slightly_drunk": {"name": "Slightly drunk", "note": ""}})
        self.assertTrue(self.world(type="clear_state", who="mira", state="slightly drunk")["ok"])
        self.assertFalse(self.world(type="set_state", state="")["ok"])

    def test_someone_away_is_out_of_reach(self):
        self.world(type="set_state", who="mira", state="away", note="gone to market")
        self.assertIn("not in the same place", self.world(type="transfer_item", item="cellar_key", **{"from": "mira", "to": "player"})["message"])
        self.assertIn("not in the same place", self.player(type="use_skill", skill="quick_strike", target="mira")["message"])
        self.assertIn("is not here", self.player(type="start_battle", enemies=["mira"])["message"])
        self.world(type="clear_state", who="mira", state="away")
        self.assertTrue(self.world(type="transfer_item", item="cellar_key", **{"from": "mira", "to": "player"})["ok"])

    def test_states_in_a_fight(self):
        self.player(type="start_battle", enemies=["marsh_bandit", "mira"])
        self.world(type="set_state", who="marsh_bandit", state="asleep")
        results = battle.take_turn(self.card, self.state, {"type": "battle_wait"}, Dice())
        self.assertEqual([r["message"] for r in results], ["Traveler holds back.", "Marsh Bandit is asleep and does nothing.", "Mira Oakhand attacks Traveler (Health -2, now 18)."])
        self.world(type="set_state", state="restrained")
        for kind in ("battle_attack", "battle_flee"):
            stopped = battle.take_turn(self.card, self.state, {"type": kind, "target": "mira"}, Dice(0.1))
            self.assertEqual([r["message"] for r in stopped], ["Traveler cannot do that while restrained."])
        self.assertEqual(len(battle.take_turn(self.card, self.state, {"type": "use_skill", "skill": "field_dressing"}, Dice())), 1)
        self.assertEqual(self.state["battle"]["round"], 2)                                      # only the wait used up a round

    def test_starting_states_and_switching_them_off(self):
        data = copy.deepcopy(self.card.data)
        data["characters"][1]["start"]["states"] = [{"state": "asleep", "note": "in the hayloft"}]
        data["states"] = [{"id": "cursed", "name": "Cursed", "blocks": ["flying"]}]
        self.assertEqual(len(check_card(data)), 1)
        data["states"][0]["blocks"] = ["skills"]
        self.assertEqual(check_card(data), [])
        state = new_game(Card(data, CARD_DIR))
        self.assertEqual(state["actors"]["tobin"]["states"], {"asleep": {"name": "Asleep", "note": "in the hayloft"}})

        data["rules"]["features"]["states"] = False
        off = Card(data, CARD_DIR)
        self.assertIn("does not use states", apply_action(off, self.state, {"type": "set_state", "state": "restrained"})["message"])
        self.me["states"]["restrained"] = {"name": "Restrained", "note": ""}
        self.assertTrue(apply_action(off, self.state, {"type": "move", "location": "stable"}, by_player=True)["ok"])   # ignored when off

    def test_old_saves_gain_states(self):
        for actor in self.state["actors"].values():
            del actor["states"]
        reconcile(self.card, self.state)
        self.assertEqual(self.me["states"], {})


if __name__ == "__main__":
    unittest.main()
