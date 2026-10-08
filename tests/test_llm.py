"""Run with: python3 -m unittest discover tests"""

import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))

from aigame import llm, prompt  # noqa: E402
from aigame.actions import apply_actions  # noqa: E402
from aigame.card import Card, load_card  # noqa: E402
from aigame.state import new_game, track  # noqa: E402

MESSAGES = [{"role": "user", "content": "hi"}]


class RequestTest(unittest.TestCase):
    def test_anthropic_request(self):
        conn = {"provider": "anthropic", "api_key": "k"}
        r = llm.chat_request(conn, "claude-opus-5-5", "sys", MESSAGES, {"temperature": 0.9, "top_k": 5, "max_tokens": 600})
        self.assertEqual(r["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(r["headers"]["x-api-key"], "k")
        self.assertEqual(r["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(r["json"]["system"], [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
        self.assertNotIn("cache_control", r["json"])                 # no automatic marker: it would land on the part that changes
        self.assertEqual(r["json"]["max_tokens"], 16000)
        self.assertEqual(r["json"]["fallbacks"], "default")
        self.assertIn("anthropic-beta", r["headers"])
        for rejected in ("temperature", "top_k", "top_p"):
            self.assertNotIn(rejected, r["json"])

    def test_anthropic_fallbacks_only_on_models_that_can_refuse(self):
        r = llm.chat_request({"provider": "anthropic", "api_key": "k"}, "claude-haiku-4-5", "", MESSAGES)
        self.assertNotIn("fallbacks", r["json"])
        self.assertNotIn("anthropic-beta", r["headers"])
        self.assertNotIn("system", r["json"])

    def test_openai_compatible_request(self):
        r = llm.chat_request({"provider": "openrouter", "api_key": "k"}, "some/model", "sys", MESSAGES, {"temperature": 0.9, "top_k": 5, "max_tokens": 600})
        self.assertEqual(r["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(r["headers"], {"Authorization": "Bearer k"})
        self.assertEqual(r["json"]["messages"][0], {"role": "system", "content": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]})
        self.assertEqual(r["json"]["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual((r["json"]["temperature"], r["json"]["top_k"], r["json"]["max_tokens"]), (0.9, 5, 600))

    def test_local_needs_no_key_and_custom_needs_an_address(self):
        r = llm.chat_request({"provider": "local", "base_url": "http://localhost:8080/v1/"}, "m", "", MESSAGES)
        self.assertEqual((r["url"], r["headers"]), ("http://localhost:8080/v1/chat/completions", {}))
        self.assertRaises(llm.LLMError, llm.chat_request, {"provider": "custom"}, "m", "", MESSAGES)
        self.assertRaises(llm.LLMError, llm.chat_request, {"provider": "anthropic"}, "m", "", MESSAGES)
        self.assertRaises(llm.LLMError, llm.chat_request, {"provider": "local"}, "", "", MESSAGES)
        self.assertNotIn("top_k", llm.chat_request({"provider": "custom", "base_url": "http://x"}, "m", "", MESSAGES, {"top_k": 5})["json"])

    def test_reading_replies(self):
        anthropic = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Hello"}, {"type": "text", "text": " there"}], "stop_reason": "end_turn"}
        self.assertEqual(llm.chat_text("anthropic", anthropic), "Hello there")
        self.assertEqual(llm.chat_text("openrouter", {"choices": [{"message": {"content": " Hi "}}]}), "Hi")
        for provider, bad in [("anthropic", {"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}}),
                              ("anthropic", {"content": []}), ("local", {"choices": []}), ("local", {"error": {"message": "nope"}}),
                              ("local", {"choices": [{"message": {"content": None}}]}), ("local", "text")]:
            self.assertRaises(llm.LLMError, llm.chat_text, provider, bad)

    def test_noticing_a_reply_that_was_cut_short(self):
        self.assertTrue(llm.chat_cut_short("anthropic", {"stop_reason": "max_tokens"}))
        self.assertFalse(llm.chat_cut_short("anthropic", {"stop_reason": "end_turn"}))
        self.assertTrue(llm.chat_cut_short("openrouter", {"choices": [{"finish_reason": "length", "message": {"content": "x"}}]}))
        self.assertFalse(llm.chat_cut_short("openrouter", {"choices": [{"finish_reason": "stop"}]}))
        for odd in ({}, {"choices": []}, "text", None):
            self.assertFalse(llm.chat_cut_short("local", odd))
        self.assertEqual(llm.estimate_tokens("a" * 350, [{"role": "user", "content": "b" * 350}]), 200)

    def test_model_list_and_errors(self):
        self.assertEqual(llm.model_ids({"data": [{"id": "b"}, {"id": "a"}, {"x": 1}]}), ["a", "b"])
        self.assertIn("API key was not accepted", llm.describe_http_error(401, '{"error": {"message": "bad key"}}'))
        self.assertIn("bad key", llm.describe_http_error(401, '{"error": {"message": "bad key"}}'))
        self.assertIn("Could not reach", llm.describe_http_error(None, None))

    def test_context_sizes_from_model_lists(self):
        listing = {"data": [
            {"id": "anthropic/claude-sonnet-5.5", "context_length": 1000000, "top_provider": {"context_length": 200000}},   # OpenRouter
            {"id": "claude-opus-5-5", "max_input_tokens": 1000000, "max_tokens": 128000},                                  # Anthropic
            {"id": "open/router-nested", "top_provider": {"context_length": 32768}},
            {"id": "llama-local", "meta": {"n_ctx_train": 8192}},                                                          # llama.cpp
            {"id": "lmstudio-model", "max_context_length": 131072},
            {"id": "says-nothing", "object": "model"},
            {"id": "nonsense", "context_length": "lots"}, {"id": "tiny", "context_length": 8}, {"no_id": True}, "junk"]}
        self.assertEqual(llm.model_contexts(listing), {"anthropic/claude-sonnet-5.5": 1000000, "claude-opus-5-5": 1000000,
                                                       "open/router-nested": 32768, "llama-local": 8192, "lmstudio-model": 131072})
        self.assertEqual(llm.model_contexts([{"id": "bare-list", "context_window": 4096}]), {"bare-list": 4096})
        self.assertEqual(llm.model_contexts({}), {})
        self.assertTrue(llm.models_request({"provider": "nanogpt", "api_key": "k"})["url"].endswith("/models?detailed=true"))
        self.assertTrue(llm.models_request({"provider": "openrouter", "api_key": "k"})["url"].endswith("/models"))

    def test_extract_json(self):
        self.assertEqual(llm.extract_json('Sure!\n```json\n{"actions": []}\n```'), {"actions": []})
        self.assertIsNone(llm.extract_json("no json here"))
        self.assertIsNone(llm.extract_json("{broken"))


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card, {"name": "Ash"})
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            self.preset = json.load(f)

    def test_state_description_has_ids_prices_and_people(self):
        text = prompt.describe_state(self.card, self.state)
        for part in ["Ash (player)", "Common Room (common_room)", "Stable (stable)", "Health (hp) 20/20", "Defense (defense) 1",
                     "Gold: 6", "Healing Draught (healing_draught) x1", "body: Travel Cloak (travel_cloak)",
                     "Mira Oakhand (mira)", "Cellar Key (cellar_key) x1", "Tobin (tobin) at Stable (stable)",
                     "The Bar (lantern_bar) sells", "Healing Draught (healing_draught) 8 Gold (2 left)"]:
            self.assertIn(part, text)

    def test_narrator_prompt_layout(self):
        self.state["history"].append({"player": "I look around.", "results": [], "narration": "You see a bar."})
        results = [{"ok": False, "message": "Ash does not have Bowl of Stew."}]
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I eat the stew and ask about the cellar.", results)
        self.assertIn("You are the narrator", system)
        self.assertIn("voice every character except Ash", system)  # {{user}} filled in
        self.assertIn("Write in second person", system)            # enabled toggle
        self.assertNotIn("one or two short paragraphs", system)    # disabled toggle
        self.assertIn("Low fantasy", system)
        self.assertIn("Mira Oakhand: Innkeeper", system)           # she is in the room when the game starts
        self.assertIn("Tobin: Stable boy", system)                 # the first objective names him
        self.assertIn("Marsh Bandit: A lean cutthroat", system)    # the card's own direction names him, so the model must know who he is
        self.assertIn("<actions>", system)                         # the mechanics text is long and never changes
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual([bool(m.get("cache")) for m in messages], [False, False, False, True, False])
        self.assertIn("Rain follows you", messages[1]["content"])
        last = messages[-1]["content"]
        for part in ["[Current game state]", "[Active quests]", "It is finished only when:", "I eat the stew", "- REJECTED: Ash does not have Bowl of Stew.",
                     "smuggler's cache"]:                           # lorebook entry triggered by "cellar"
            self.assertIn(part, last)
        self.assertNotIn("the lights", system + last)              # untriggered entry stays out
        for changing in ["[Current game state]", "[Active quests]", "[Background knowledge]"]:
            self.assertNotIn(changing, system)
        self.assertNotIn("{{user}}", system + last)

    def test_default_preset_keeps_the_narrator_off_the_players_character(self):
        self.state["history"].append({"player": "I look around.", "results": [], "narration": "You see a bar."})
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I sit down.", [])
        self.assertIn("Ash belongs to the player alone", system)
        self.assertIn("stop at the first point where Ash would have to act", system)
        last = messages[-1]["content"]
        self.assertIn("Do not act, speak or decide for Ash", last)
        self.assertTrue(last.index("[Current game state]") < last.index("I sit down.") < last.index("Do not act, speak or decide for Ash"))
        reminder = self.preset["blocks"][-1]["content"].replace("{{user}}", "Ash")
        own = "\n\n" + prompt.prompt_text(None, "card_reminder") if self.card.data["world"].get("narrator_instructions") else ""
        self.assertTrue(last.rstrip().endswith(reminder + own))      # the reminders are the very last thing the model reads, the card's own last of all
        self.assertTrue(last.index("[Current game state]") < last.index("(Reminder:"))
        for block in self.preset["blocks"]:
            if block["id"] in ("player_agency", "turn_reminder"):
                block["enabled"] = False
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I sit down.", [])
        self.assertNotIn("belongs to the player alone", system)
        self.assertNotIn("(Reminder:", messages[-1]["content"])

    def test_placeholders_are_filled(self):
        self.assertEqual(prompt.fill(self.card, self.state, "{{user}} owes {{user}}'s host 5 {{currency}}."), "Ash owes Ash's host 5 Gold.")
        self.assertEqual(prompt.fill(self.card, self.state, "{{char}} waves."), "{{char}} waves.")          # three characters: no telling which
        cafe = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        del cafe.characters["eli"]
        self.assertEqual(prompt.fill(cafe, new_game(cafe), "{{char}} waves at {{user}}."), "Noor waves at Sam.")

    def test_game_mechanics_block_cannot_be_disabled(self):
        for block in self.preset["blocks"]:
            block["enabled"] = False
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "Hi", [])
        self.assertIn("<actions>", system + messages[-1]["content"])
        self.assertEqual(len(messages), 3)
        self.preset["blocks"] = []
        self.assertIn("<actions>", prompt.narrator_prompt(self.card, self.state, self.preset, "Hi", [])[0])

    def test_narration_is_split_from_its_actions(self):
        story, actions = prompt.parse_narration('Mira slides a key over.\n\n<actions>{"actions": [{"type": "transfer_item", "item": "cellar_key", "from": "mira", "to": "player"}]}</actions>')
        self.assertEqual(story, "Mira slides a key over.")
        results = apply_actions(self.card, self.state, actions)
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(self.state["actors"]["player"]["inventory"]["cellar_key"], 1)
        self.assertEqual(prompt.parse_narration("Plain story."), ("Plain story.", []))
        self.assertEqual(prompt.parse_narration('Cut off. <actions>{"actions": [{"ty'), ("Cut off.", []))
        self.assertEqual(prompt.parse_narration("Bad. <actions>not json</actions>"), ("Bad.", []))

    def test_player_cannot_grant_themselves_things(self):
        reply = json.dumps({"actions": [
            {"type": "change_money", "amount": 1000},
            {"type": "add_item", "item": "cellar_key"},
            {"type": "quest_advance", "quest": "missing_courier"},
            {"type": "use_item", "item": "healing_draught", "who": "mira"},
            {"type": "transfer_item", "item": "cellar_key", "from": "mira", "to": "player"},
            "junk",
        ]})
        actions = prompt.parse_resolver(reply, self.card)
        self.assertEqual(actions, [{"type": "use_item", "item": "healing_draught"},
                                   {"type": "transfer_item", "item": "cellar_key", "from": "player", "to": "player"}])
        results = apply_actions(self.card, self.state, actions)
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertEqual(prompt.parse_resolver("I am not sure.", self.card), [])

    def test_suggestions_and_summary(self):
        self.assertEqual(prompt.parse_suggestions('{"choices": ["a", " b ", 3, "", "c", "d"]}', 3), ["a", "b", "c"])
        self.assertEqual(prompt.parse_suggestions("nothing", 3), [])
        system, messages = prompt.suggest_prompt(self.card, self.state, 3)
        self.assertIn("Rain follows you", messages[0]["content"])
        turns = [{"player": "I wave.", "results": [], "narration": "Mira nods at {{user}}."}]
        self.assertIn("Mira nods at Ash.", prompt.summary_prompt(self.card, self.state, turns)[1][0]["content"])


class DirectorTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)

    def test_paragraphs_and_prompt(self):
        paragraphs = prompt.split_paragraphs("One.\n\n  \nTwo line\nstill two.\n\n\nThree.")
        self.assertEqual(paragraphs, ["One.", "Two line\nstill two.", "Three."])
        system, messages = prompt.director_prompt(self.card, self.state, paragraphs)
        self.assertIn("Mira Oakhand (id: mira). Looks: Broad-shouldered woman", messages[0]["content"])
        self.assertIn("apron. Expressions: angry, happy, neutral, worried", messages[0]["content"])
        self.assertIn("3. Three.", messages[0]["content"])

    def test_direction_is_validated(self):
        reply = json.dumps({"paragraphs": [
            {"n": 1, "speaker": None, "expression": None},
            {"n": 2, "speaker": "mira", "expression": "angry"},
            {"n": 3, "speaker": "mira", "expression": "ecstatic"},
            {"n": 4, "speaker": "dragon", "expression": "angry"},
            {"n": 9, "speaker": "mira", "expression": "happy"},
        ]})
        self.assertEqual(prompt.parse_direction(reply, self.card, 4), [
            {"speaker": None, "expression": None},
            {"speaker": "mira", "expression": "angry"},
            {"speaker": "mira", "expression": "neutral"},
            {"speaker": None, "expression": None},
        ])
        self.assertEqual(prompt.parse_direction("no idea", self.card, 2), [{"speaker": None, "expression": None}] * 2)

    def test_long_paragraphs_are_split_at_sentences(self):
        text = " ".join("Sentence number %d is here." % n for n in range(40))
        pieces = prompt.split_for_display(text, 200)
        self.assertEqual(" ".join(pieces), text)
        self.assertTrue(all(len(p) <= 200 for p in pieces))
        self.assertEqual(prompt.split_for_display("Short."), ["Short."])


class PromptFollowsFeaturesTest(unittest.TestCase):
    def test_full_card_teaches_everything(self):
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(card)
        text = prompt.describe_state(card, state)
        for part in ["Level 1 (experience 0/50)", "Mana (mana) 10/10", "Marsh Light (marsh_light) costs Mana 4 [locked]", "Trust toward the player: 20/100"]:
            self.assertIn(part, text)
        protocol = prompt.action_protocol(card)
        for kind in ["gain_xp", "use_skill", "unlock_skill", "change_relationship", "change_money", "add_item"]:
            self.assertIn(kind, protocol)
        self.assertIn("use_skill", prompt.player_action_types(card))
        self.assertIn("use_skill", prompt.resolver_prompt(card, state, "I strike")[0])
        kept = prompt.parse_resolver('{"actions": [{"type": "use_skill", "skill": "quick_strike", "target": "mira", "who": "mira"}, {"type": "gain_xp", "amount": 999}]}', card)
        self.assertEqual(kept, [{"type": "use_skill", "skill": "quick_strike", "target": "mira"}])

    def test_casual_card_mentions_only_what_it_tracks(self):
        card = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        state = new_game(card)
        text = prompt.describe_state(card, state)
        self.assertIn("Friendship toward the player: 30/100", text)
        for absent in ["Gold", "Inventory", "Equipped", "Stats", "Level", "Skills", "All items"]:
            self.assertNotIn(absent, text)
        protocol = prompt.action_protocol(card)
        self.assertIn("tracks the state each character is in, relationships, locations.", protocol)
        self.assertIn("change_relationship", protocol)
        for absent in ["add_item", "change_money", "change_stat", "gain_xp", "quest_start"]:
            self.assertNotIn(absent, protocol)
        self.assertEqual(prompt.player_action_types(card), {"move"})

    def test_card_that_tracks_nothing_has_no_mechanics_prompt(self):
        card = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        card.data["rules"]["features"]["relationships"] = False
        card.data["rules"]["features"]["states"] = False
        card.locations.clear()
        self.assertEqual(prompt.action_protocol(card), "")
        self.assertEqual(prompt.player_action_types(card), set())
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            preset = json.load(f)
        state = new_game(card)
        system, messages = prompt.narrator_prompt(card, state, preset, "Hello", [])
        self.assertNotIn("<actions>", system + messages[-1]["content"])


class StatesPromptTest(unittest.TestCase):
    def test_narrator_is_told_states_and_what_they_stop(self):
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(card)
        apply_actions(card, state, [{"type": "set_state", "state": "restrained", "note": "tied to a chair"},
                                    {"type": "set_state", "who": "mira", "state": "away", "note": "gone to market"}])
        text = prompt.describe_state(card, state)
        self.assertIn("State: Restrained (tied to a chair)", text)
        self.assertIn("Mira Oakhand (mira) is away: Away (gone to market)", text)
        self.assertNotIn("Characters here", text)
        self.assertIn("Mira Oakhand: Innkeeper", prompt.describe_cast(card))
        protocol = prompt.action_protocol(card)
        for part in ["set_state", "clear_state", "[States]", "Restrained (restrained)", "stops every action", "they are out of the scene", '"stops": [...]']:
            self.assertIn(part, protocol)
        self.assertIn("So they cannot act: nothing they try to do gets done (restrained).", text)
        self.assertNotIn("cannot speak", text)                                                      # tied up, and can still talk
        apply_actions(card, state, [{"type": "set_state", "state": "Mouth shut", "note": "sealed by a spell", "stops": ["speech", "flying"]},
                                    {"type": "set_state", "who": "tobin", "state": "blindfolded", "stops": ["sight"]}])
        text = prompt.describe_state(card, state)
        self.assertIn("cannot speak: whatever they try to say comes out as muffled or wordless sound, and nobody makes out the words (mouth shut)", text)
        self.assertIn("tell the attempt and how it fails, never the thing done", text)
        self.assertEqual(state["actors"]["player"]["states"]["mouth_shut"]["stops"], ["speech"])    # what is not a real limit is dropped
        notes = prompt.sense_notes(card, state)                                                     # how the player's own message lands this turn
        self.assertEqual([n["ok"] for n in notes], [None])
        for part in ["cannot speak (mouth shut)", "what they meant, not what anyone heard", "a guess can be wrong", "never the wording"]:
            self.assertIn(part, notes[0]["message"])
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            sent = prompt.narrator_prompt(card, state, json.load(f), 'I shout "Run, Mira!"', notes)[1][-1]["content"]
        self.assertIn("UP TO YOU: %s cannot speak" % state["actors"]["player"]["name"], sent)
        apply_actions(card, state, [{"type": "clear_state", "state": "mouth shut"}])
        self.assertEqual(prompt.sense_notes(card, state), [])
        self.assertNotIn("set_state", prompt.resolver_prompt(card, state, "I break free")[0])       # the player cannot free themselves by saying so
        self.assertEqual(prompt.parse_resolver('{"actions": [{"type": "clear_state", "state": "restrained"}]}', card), [])

    def test_cards_without_states_never_mention_them(self):
        card = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        card.data["rules"]["features"]["states"] = False
        protocol = prompt.action_protocol(card)
        for absent in ("set_state", "clear_state", "[States]", "Restrained"):
            self.assertNotIn(absent, protocol)
        self.assertNotIn("State:", prompt.describe_state(card, new_game(card)))


class LeavingTest(unittest.TestCase):
    def test_narrator_is_told_what_leaving_means(self):
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(card)
        card.quests["missing_courier"]["fail_when"] = "the player leaves the inn for good"
        results = apply_actions(card, state, [{"type": "move", "location": "stable"}], by_player=True)
        self.assertEqual(results[0]["message"], "Traveler leaves Common Room and goes to Stable.")
        self.assertIn("Fails if: the player leaves the inn for good", prompt.describe_quests(card, state))
        protocol = prompt.action_protocol(card)
        for part in ["[When {{user}} leaves a place]", "put them back with a move action", "quest_fail"]:
            self.assertIn(part, protocol)
        # the narrator sending them back is an ordinary move, and it is allowed
        back = apply_actions(card, state, prompt.parse_narration('A hand lands on your shoulder.\n<actions>{"actions": [{"type": "move", "who": "player", "location": "common_room"}]}</actions>')[1])
        self.assertTrue(back[0]["ok"])
        self.assertEqual(state["actors"]["player"]["location"], "common_room")

    def test_far_travel_is_handed_to_the_story(self):
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(card)
        apply_actions(card, state, [{"type": "move", "location": "stable"}], by_player=True)
        results = apply_actions(card, state, [{"type": "move", "location": "cellar"}], by_player=True)
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            preset = json.load(f)
        system, messages = prompt.narrator_prompt(card, state, preset, "I step through the portal to the cellar.", results, record=False)
        self.assertIn("- UP TO YOU: Traveler wants to go to Cellar, which is not next to Stable.", messages[-1]["content"])
        self.assertIn('"UP TO YOU" is something the engine left to the story', system)
        keeper = prompt.bookkeeper_prompt(card, state, "I step through the portal.", results, "The portal closes behind you in the cellar.")[0]
        self.assertIn("however they got there and however far it is", keeper)
        self.assertIn("Any location can be reached this way", keeper)

    def test_narrator_can_close_the_map(self):
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(card)
        self.assertEqual(apply_actions(card, state, [{"type": "lock_travel", "reason": "The exam has begun and the captains are watching."}])[0]["message"],
                         "Traveler can no longer leave: The exam has begun and the captains are watching.")
        tried = apply_actions(card, state, [{"type": "move", "location": "stable"}], by_player=True)[0]
        # Asked for after play: a teleporter, or someone who simply will not stay, is not stopped by the
        # game. The map stays closed to walking, and the attempt is handed to the story with why they are held.
        self.assertIsNone(tried["ok"])
        self.assertIn("is held where they are (The exam has begun and the captains are watching) and tries to go to Stable all the same", tried["message"])
        self.assertEqual(state["actors"]["player"]["location"], "common_room")
        self.assertIn("Travel: LOCKED for the player (The exam has begun and the captains are watching.)", prompt.describe_state(card, state))
        moved = apply_actions(card, state, [{"type": "move", "who": "player", "location": "cellar"}])[0]                    # the story can still move them
        self.assertTrue(moved["ok"])
        self.assertIsNone(state["travel_lock"])                                                                             # and what held them there holds them no longer
        self.assertIn("Nothing holds them there any more.", moved["message"])
        apply_actions(card, state, [{"type": "lock_travel", "reason": "The exam has begun and the captains are watching."}])
        self.assertTrue(apply_actions(card, state, [{"type": "move", "who": "tobin", "location": "common_room"}], by_player=False)[0]["ok"])
        self.assertTrue(apply_actions(card, state, [{"type": "unlock_travel"}])[0]["ok"])
        self.assertFalse(apply_actions(card, state, [{"type": "unlock_travel"}])[0]["ok"])
        self.assertTrue(apply_actions(card, state, [{"type": "move", "location": "common_room"}], by_player=True)[0]["ok"])
        self.assertNotIn("Travel:", prompt.describe_state(card, state))
        self.assertEqual(apply_actions(card, state, [{"type": "lock_travel"}])[0]["message"], "Traveler can no longer leave: something is keeping them here.")
        for part in ["lock_travel", "unlock_travel", "Never leave it locked"]:
            self.assertIn(part, prompt.action_protocol(card))
        # the player cannot lift it by saying so
        self.assertEqual(prompt.parse_resolver('{"actions": [{"type": "unlock_travel"}]}', card), [])

    def test_cards_without_a_map_are_not_told_about_leaving(self):
        card = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        card.locations.clear()
        self.assertNotIn("leaves a place", prompt.action_protocol(card))
        self.assertNotIn("lock_travel", prompt.action_protocol(card))


class FormattingTest(unittest.TestCase):
    def test_marks_become_tags(self):
        from aigame.text import to_renpy
        cases = {
            "*She smiles.* \"Fine.\"": "{i}She smiles.{/i} \"Fine.\"",
            "A **very** bad idea": "A {b}very{/b} bad idea",
            "***Run!***": "{b}{i}Run!{/i}{/b}",
            "_quietly_ and __loudly__": "{i}quietly{/i} and {b}loudly{/b}",
            "~~gone~~ `key` here": "{s}gone{/s} key here",
            "# The Cellar\nDark.": "{b}The Cellar{/b}\nDark.",
            "2 * 3 * 4 and snake_case_name": "2 * 3 * 4 and snake_case_name",          # not formatting
            "a lone * star and [brackets] {braces}": "a lone * star and [[brackets] {{braces}",
            "*two\nlines*": "*two\nlines*",                                             # italics do not cross a line break
            "*a **b* c**": "*a **b* c**",                                               # overlapping marks are left as typed
        }
        for typed, shown in cases.items():
            self.assertEqual(to_renpy(typed), shown, typed)

    def test_a_span_cut_across_pieces_formats_in_each(self):
        from aigame.text import to_renpy
        text = "*" + " ".join("Sentence number %d is here." % n for n in range(12)) + "* Then **bold one. And bold two.** Done."
        pieces = prompt.split_for_display(text, 90)
        self.assertGreater(len(pieces), 3)
        for piece in pieces:
            shown = to_renpy(piece)
            self.assertNotIn("*", shown, piece)
        self.assertTrue(to_renpy(pieces[0]).startswith("{i}Sentence number 0") and to_renpy(pieces[1]).startswith("{i}"))
        self.assertTrue(any("{b}" in to_renpy(p) for p in pieces))
        self.assertEqual(prompt.split_for_display("Plain text. No marks."), ["Plain text. No marks."])


class CachingTest(unittest.TestCase):
    """Providers only bill the start of a prompt cheaply if it is byte-for-byte what they saw last
    time. These play several turns and compare what would be sent."""

    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            self.preset = json.load(f)

    def request(self, text, provider="anthropic", model="claude-opus-5-5"):
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, text, [])
        return llm.chat_request({"provider": provider, "api_key": "k", "base_url": "http://x/v1"}, model, system, messages, self.preset["sampling"])["json"]

    def finish_turn(self, text, narration, actions=()):
        entering = prompt.arrivals(self.card, self.state, text)        # as the game does: whoever enters with this turn is kept with it
        results = apply_actions(self.card, self.state, list(actions))
        self.state["history"].append({"player": text, "results": [{"ok": r["ok"], "message": r["message"]} for r in results], "narration": narration, "cast": entering})
        self.state["cast"] += entering
        self.state["turn"] += 1

    def cached_prefix(self, body):
        """Everything up to and including the last cache marker, as the bytes a provider would compare."""
        marked = max(n for n, m in enumerate(body["messages"]) if isinstance(m["content"], list))
        return json.dumps([body.get("system"), body["messages"][:marked + 1]], sort_keys=True)

    def unmarked(self, value):
        """The same content with the cache markers taken off, so two requests can be compared by what they say."""
        if isinstance(value, list):
            if len(value) == 1 and isinstance(value[0], dict) and value[0].get("type") == "text":
                return value[0]["text"]
            return [self.unmarked(v) for v in value]
        if isinstance(value, dict):
            return dict((k, self.unmarked(v)) for k, v in value.items())
        return value

    def test_each_turn_extends_the_last_one(self):
        # Turns that move the player, change who is present, trigger lore, change stats, quests and states.
        script = [("I ask about the cellar.", "Mira shrugs.", [{"type": "change_relationship", "who": "mira", "amount": 5}]),
                  ("I go to the stable.", "Hay and horse.", [{"type": "move", "location": "stable"}, {"type": "change_stat", "stat": "hp", "amount": -3}]),
                  ("I ask Tobin about the marsh lights.", "He goes pale.", [{"type": "quest_advance", "quest": "missing_courier"}, {"type": "set_state", "who": "tobin", "state": "frightened"}]),
                  ("I buy him a drink.", "He nods.", [{"type": "move", "location": "common_room"}, {"type": "gain_xp", "amount": 60}])]
        previous = self.request(script[0][0])
        self.assertEqual(len(previous["system"]), 1)
        for (text, narration, actions), following in zip(script, [s[0] for s in script[1:]] + ["I wait."]):
            self.finish_turn(text, narration, actions)
            current = self.request(following)
            self.assertEqual(current["system"], previous["system"], "the instructions changed after: " + text)
            # What was marked for caching last turn is still the start of this turn's prompt.
            before = self.unmarked(json.loads(self.cached_prefix(previous)))
            now = self.unmarked([current["system"], current["messages"][:len(before[1])]])
            self.assertEqual(now, before, "the cached part changed after: " + text)
            self.assertGreater(len(self.cached_prefix(current)), len(self.cached_prefix(previous)))
            previous = current

    def test_only_the_last_message_holds_what_changes(self):
        self.finish_turn("I ask about the cellar.", "Mira shrugs.")
        body = self.request("I ask about the marsh lights.")
        tail = body["messages"][-1]
        self.assertIsInstance(tail["content"], str)                                   # not marked: it is new every turn
        for part in ["[Current game state]", "[Active quests]", "[Background knowledge]", "I ask about the marsh lights."]:
            self.assertIn(part, tail["content"])
        self.assertEqual(sum(isinstance(m["content"], list) for m in body["messages"]), 1)
        self.assertEqual(body["messages"][-2]["content"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_summarizing_keeps_the_instructions_cached(self):
        for n in range(6):
            self.finish_turn("Line %d." % n, "Reply %d." % n)
        before = self.request("Next.")
        self.state["summary"], self.state["summarized"] = "Earlier, the traveler arrived and talked with Mira.", 4
        after = self.request("Next.")
        self.assertEqual(after["system"], before["system"])                           # the long instructions are untouched
        self.assertNotIn("Story so far", json.dumps(after["system"]))
        self.assertIn("[Story so far]\nEarlier, the traveler arrived", after["messages"][-1]["content"])
        self.assertLess(len(after["messages"]), len(before["messages"]))              # the folded turns are no longer sent

    def test_a_preset_cannot_put_changing_blocks_in_the_cached_part(self):
        blocks = self.preset["blocks"]
        self.preset["blocks"] = [b for b in blocks if b.get("slot") in ("state", "quests", "lorebook")] + [b for b in blocks if b.get("slot") not in ("state", "quests", "lorebook")]
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I ask about the cellar.", [])
        self.assertNotIn("[Current game state]", system)
        self.assertIn("[Current game state]", messages[-1]["content"])

    def test_markers_per_provider(self):
        self.finish_turn("Hello.", "Mira nods.")
        def marks(provider, model):
            body = self.request("Again.", provider, model)
            system = body["system"] if provider == "anthropic" else body["messages"][0]["content"]
            return (system[0].get("cache_control") if isinstance(system, list) else None,
                    [m["content"][0]["cache_control"] for m in body["messages"][1 if provider != "anthropic" else 0:] if isinstance(m["content"], list)])
        long, short = {"type": "ephemeral", "ttl": "1h"}, {"type": "ephemeral"}
        self.assertEqual(marks("anthropic", "claude-opus-5-5"), (long, [long]))
        self.assertEqual(marks("openrouter", "anthropic/claude-sonnet-5.5"), (long, [long]))
        self.assertEqual(marks("openrouter", "deepseek/deepseek-chat"), (long, [long]))       # OpenRouter ignores or translates it
        self.assertEqual(marks("nanogpt", "claude-sonnet-4-5"), (short, [short]))
        for provider, model in (("nanogpt", "gpt-5"), ("custom", "whatever"), ("local", "llama")):
            body = self.request("Again.", provider, model)
            self.assertTrue(all(isinstance(m["content"], str) for m in body["messages"]), provider)   # plain text, no unknown fields
            self.assertNotIn("cache", json.dumps(body))

    def test_reading_what_was_billed(self):
        self.assertEqual(llm.chat_usage("anthropic", {"usage": {"input_tokens": 300, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 700, "output_tokens": 250}}),
                         {"input": 10000, "cached": 9000, "written": 700, "output": 250})
        self.assertEqual(llm.chat_usage("openrouter", {"usage": {"prompt_tokens": 10339, "completion_tokens": 60, "prompt_tokens_details": {"cached_tokens": 10318, "cache_write_tokens": 0}}}),
                         {"input": 10339, "cached": 10318, "written": 0, "output": 60})
        self.assertEqual(llm.chat_usage("nanogpt", {"usage": {"prompt_tokens": 500, "completion_tokens": 5, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 50}}),
                         {"input": 500, "cached": 400, "written": 50, "output": 5})
        for nothing in ({}, {"usage": None}, {"usage": {"prompt_tokens": "many"}}, "text"):
            self.assertEqual(llm.chat_usage("local", nothing), {"input": 0, "cached": 0, "written": 0, "output": 0})


class TrackerTest(unittest.TestCase):
    def setUp(self):
        from aigame.state import track
        self.track = track
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        self.actors = self.state["actors"]

    def test_director_is_told_where_everyone_was(self):
        system, messages = prompt.director_prompt(self.card, self.state, ["Tobin slips in from the yard."])
        text = messages[0]["content"]
        for part in ["Mira Oakhand (id: mira)", "Before this text: with the player", "Tobin (id: tobin)", "Before this text: at Stable (stable)",
                     "Marsh Bandit (id: marsh_bandit)", "not in the story yet", "The player is at Common Room (common_room).", "Cellar (cellar)"]:
            self.assertIn(part, text)
        self.assertIn('"whereabouts"', system)

    def test_whereabouts_follow_the_story(self):
        reply = json.dumps({"paragraphs": [{"n": 1, "speaker": "tobin", "expression": "worried"}], "whereabouts": [
            {"id": "tobin", "location": "here", "note": "came in from the yard, out of breath"},
            {"id": "mira", "location": "cellar", "note": "gone down for a cask"},
            {"id": "marsh_bandit", "location": None, "note": "watching the inn from the marsh road"},
            {"id": "player", "location": "cellar"}, {"id": "dragon", "location": "here"}, {"id": "tobin"}, "junk"]})
        updates = prompt.parse_whereabouts(reply, self.card)
        self.assertEqual([u["id"] for u in updates], ["tobin", "mira", "marsh_bandit"])
        changed = self.track(self.card, self.state, updates)
        self.assertEqual(changed, ["Tobin: Common Room (came in from the yard, out of breath)", "Mira Oakhand: Cellar (gone down for a cask)",
                                   "Marsh Bandit: off the map (watching the inn from the marsh road)"])
        self.assertEqual((self.actors["tobin"]["location"], self.actors["mira"]["location"], self.actors["player"]["location"]), ("common_room", "cellar", "common_room"))
        self.assertEqual(prompt.parse_direction(reply, self.card, 1), [{"speaker": "tobin", "expression": "worried"}])   # the same reply still directs the scene
        text = prompt.describe_state(self.card, self.state)
        self.assertIn("- Tobin (tobin) [came in from the yard, out of breath]", text)
        self.assertIn("Mira Oakhand (mira) at Cellar (cellar) [gone down for a cask]", text)
        self.assertIn("Marsh Bandit (marsh_bandit) at an unknown place [watching the inn from the marsh road]", text)

    def test_bad_places_are_ignored_but_notes_kept(self):
        updates = prompt.parse_whereabouts('{"whereabouts": [{"id": "mira", "location": "the moon", "note": "humming"}, {"id": "tobin", "location": 7}]}', self.card)
        self.assertEqual(updates, [{"id": "mira", "note": "humming"}])
        self.assertEqual(self.track(self.card, self.state, updates), ["Mira Oakhand: Common Room (humming)"])
        self.assertEqual(self.actors["mira"]["location"], "common_room")
        self.assertEqual(prompt.parse_whereabouts("no idea", self.card), [])
        self.assertEqual(self.track(self.card, self.state, []), [])

    def test_strangers_become_known_when_the_story_brings_them_in(self):
        from aigame.state import meet
        data = json.loads(json.dumps(self.card.data))
        for c in data["characters"]:
            c.setdefault("start", {})["known"] = False
        from aigame.card import Card, check_card
        self.assertEqual(check_card(data), [])
        card = Card(data, self.card.path)
        state = new_game(card)
        known = lambda: sorted(w for w, a in state["actors"].items() if a["known"] and w != "player")
        self.assertEqual(known(), [])
        self.assertIn("Mira Oakhand (mira) [not met yet]", prompt.describe_state(card, state))
        meet(state, ["mira", "nobody"])                                                           # she speaks in a scene
        self.track(card, state, [{"id": "tobin", "location": "here"}, {"id": "marsh_bandit", "note": "watching from the road"}])
        self.assertEqual(known(), ["mira", "tobin"])                                              # the bandit was only mentioned at a distance
        self.assertNotIn("Mira Oakhand (mira) [not met yet]", prompt.describe_state(card, state))
        self.assertTrue(all(a["known"] for a in new_game(self.card)["actors"].values()))          # the default is that everyone is known

    def test_old_saves_gain_the_note(self):
        from aigame.state import reconcile
        for actor in self.actors.values():
            del actor["note"]
        reconcile(self.card, self.state)
        self.assertEqual(self.actors["mira"]["note"], "")


class HiddenPlacesTest(unittest.TestCase):
    def setUp(self):
        from aigame.card import Card, check_card
        base = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        data = json.loads(json.dumps(base.data))
        for location in data["locations"]:
            if location["id"] in ("cellar", "common_room"):
                location["hidden"] = True                       # the start location is marked hidden too, to check it is shown anyway
        self.assertEqual(check_card(data), [])
        self.card = Card(data, base.path)
        self.state = new_game(self.card)

    def act(self, by_player=False, **action):
        return apply_actions(self.card, self.state, [action], by_player=by_player)[0]

    def test_hidden_places_start_off_the_map(self):
        self.assertEqual(self.state["revealed"], ["common_room", "stable"])
        tried = self.act(by_player=True, type="move", location="cellar")
        # Seen in play: the player was being taken through a portal to a hideout that was off their map,
        # and was flatly refused. They cannot walk there alone, but the story may take them: it is left to the story.
        self.assertIsNone(tried["ok"])
        self.assertIn("means to go to Cellar, which is not on their map", tried["message"])
        self.assertEqual(self.state["actors"]["player"]["location"], "common_room")                  # the game itself moves nobody
        self.assertEqual(self.state["revealed"], ["common_room", "stable"])                          # and the map is not filled in by asking
        self.assertIsNone(self.act(by_player=True, type="move", location="Cellar")["ok"])            # the same by its name
        self.assertFalse(self.act(by_player=True, type="move", location="atlantis")["ok"])           # a place that does not exist is still refused
        self.act(type="move", who="player", location="cellar")                                       # the story takes them there:
        self.assertIn("cellar", self.state["revealed"])                                              # now it is on the map
        self.act(type="move", who="player", location="common_room")
        self.assertTrue(self.act(by_player=True, type="move", location="stable")["ok"])

    def test_narrator_is_told_and_can_reveal(self):
        text = prompt.describe_state(self.card, self.state)
        self.assertIn("Cellar (cellar) [the player does not know of it yet]", text)
        self.assertIn("Places the player does not know of yet", text)
        self.assertIn("Cellar (cellar): Cold stone", text)
        self.assertIn("reveal_location", prompt.action_protocol(self.card))
        self.assertEqual(self.act(type="reveal_location", location="cellar")["message"], "The map now shows Cellar.")
        self.assertFalse(self.act(type="reveal_location", location="cellar")["ok"])
        self.assertNotIn("does not know of", prompt.describe_state(self.card, self.state))
        self.assertTrue(self.act(by_player=True, type="move", location="cellar")["ok"])

    def test_being_taken_somewhere_reveals_it(self):
        self.assertTrue(self.act(type="move", who="player", location="cellar")["ok"])
        self.assertIn("cellar", self.state["revealed"])

    def test_helpers_that_speak_for_the_player_are_not_told(self):
        for system, messages in (prompt.resolver_prompt(self.card, self.state, "I look around."), prompt.suggest_prompt(self.card, self.state, 3)):
            self.assertNotIn("Cellar (cellar)", messages[0]["content"])               # the place; Mira's Cellar Key is a separate thing
            self.assertNotIn("does not know of", messages[0]["content"])
        self.assertEqual(prompt.parse_resolver('{"actions": [{"type": "reveal_location", "location": "cellar"}]}', self.card), [])

    def test_scene_director_reveals_from_the_text(self):
        from aigame.state import reveal
        system, messages = prompt.director_prompt(self.card, self.state, ["Mira nods at the locked door. \"The cellar is down there.\""])
        self.assertIn("[Places the player does not know of yet]\n- Cellar (cellar): Cold stone", messages[0]["content"])
        self.assertIn('"revealed"', system)
        found = prompt.parse_revealed('{"paragraphs": [], "revealed": ["cellar", "atlantis", 7, "stable"]}', self.card)
        self.assertEqual(found, ["cellar", "stable"])
        self.assertEqual(reveal(self.card, self.state, found), ["Cellar"])                           # the stable was already known
        self.assertEqual(prompt.parse_revealed("no json", self.card), [])
        self.assertNotIn("Places the player does not know", prompt.director_prompt(self.card, self.state, ["x"])[1][0]["content"])

    def test_cards_without_hidden_places_are_unchanged(self):
        plain = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(plain)
        self.assertEqual(state["revealed"], ["common_room", "stable", "cellar"])
        self.assertNotIn("does not know of", prompt.describe_state(plain, state))
        del state["revealed"]                                                                         # a save from before this existed
        from aigame.state import reconcile
        reconcile(plain, state)
        self.assertEqual(state["revealed"], ["common_room", "stable", "cellar"])


class BookkeeperTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            self.preset = json.load(f)

    def test_story_model_is_asked_only_for_the_story(self):
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I cast a light.", [], record=False)
        whole = system + messages[-1]["content"]
        for absent in ["<actions>", '{"type":', "change_stat", "set_state", "reveal_location", "lock_travel"]:
            self.assertNotIn(absent, whole, absent)
        for present in ["A bookkeeper reads what you write", "REJECTED", "is no longer flying", "Restrained (restrained)", "they are stopped and still where they were"]:
            self.assertIn(present, whole, present)
        with_actions = prompt.narrator_prompt(self.card, self.state, self.preset, "I cast a light.", [])[0]
        self.assertIn("<actions>", with_actions)
        self.assertLess(len(system), len(with_actions))                               # less for a small model to carry

    def test_a_streamed_reply_reads_the_same_as_a_whole_one(self):
        connection = {"provider": "nanogpt", "base_url": "", "api_key": "k"}
        asked = llm.stream_request(connection, "m", "sys", [{"role": "user", "content": "hi"}], {"max_tokens": 50})["json"]
        self.assertEqual((asked["stream"], asked["stream_options"]), (True, {"include_usage": True}))
        self.assertNotIn("stream_options", llm.stream_request(dict(connection, provider="custom", base_url="http://x/v1"), "m", "s", [{"role": "user", "content": "hi"}])["json"])

        reader = llm.StreamReader("nanogpt")
        for line in (b': keep-alive', b'data: {"choices": [{"delta": {"role": "assistant", "content": ""}}]}', b'data: {"choices": [{"delta": {"content": "Rain "}}]}', b'',
                     'data: {"choices": [{"delta": {"content": "falls."}}]}', b'data: not json', b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
                     b'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}}', b'data: [DONE]'):
            reader.feed(line)
        self.assertEqual((reader.text, reader.done), ("Rain falls.", True))
        whole = reader.data()
        self.assertEqual(llm.chat_text("nanogpt", whole), "Rain falls.")
        self.assertFalse(llm.chat_cut_short("nanogpt", whole))
        self.assertEqual(llm.chat_usage("nanogpt", whole), {"input": 9, "cached": 4, "written": 0, "output": 3})
        self.assertTrue(llm.chat_cut_short("nanogpt", reader.data(cut_off=True)))            # what arrived before a dropped connection is kept, marked as cut short

        claude = llm.StreamReader("anthropic")
        for line in ('event: message_start', 'data: {"type": "message_start", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 20}}}',
                     'data: {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}',
                     'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Rain."}}',
                     'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}', 'data: {"type": "message_stop"}'):
            claude.feed(line)
        self.assertEqual((llm.chat_text("anthropic", claude.data()), claude.done), ("Rain.", True))
        self.assertEqual(llm.chat_usage("anthropic", claude.data()), {"input": 25, "cached": 20, "written": 0, "output": 2})
        failed = llm.StreamReader("nanogpt")
        failed.feed('data: {"error": {"message": "overloaded"}}')
        self.assertRaises(llm.LLMError, llm.chat_text, "nanogpt", failed.data())

    def test_a_judge_that_answers_in_an_odd_shape_does_not_stop_the_game(self):
        """Seen in play: a model listed the quests to start as objects, and the turn crashed."""
        quest = next(iter(self.card.quests))
        odd = json.dumps({"verdicts": [{"quest": {"id": quest}, "verdict": "done", "objective": "x"}, {"quest": [quest], "verdict": "failed"}, "done"],
                          "start": [{"quest": quest}, {"id": quest}, {"name": "?"}, [quest], 7, None, quest]})
        self.assertEqual(prompt.parse_judge(odd, self.card), [{"type": "quest_start", "quest": quest}] * 3)

    def test_the_story_model_is_shown_who_was_there_for_each_turn(self):
        """Seen in play: a character who had only just arrived spoke of what the player had done before he came."""
        self.state["history"] += [
            {"player": "I vanish the cart.", "results": [], "narration": "It is gone.", "with": ["mira"], "there": ["mira"]},
            {"player": "I wait.", "results": [], "narration": "Tobin comes in.", "with": ["mira"], "there": ["mira", "tobin"]},
            {"player": "Hello.", "results": [], "narration": "He nods.", "with": ["mira"], "direction": [{"speaker": "tobin", "expression": "neutral"}]},
            {"player": "Old turn.", "results": [], "narration": "From before the game kept track."}]
        self.state["actors"]["tobin"]["location"] = self.state["actors"]["player"]["location"]
        system, messages = prompt.narrator_prompt(self.card, self.state, self.preset, "I ask Tobin what he saw.", [])
        asked = [m["content"] for m in messages if m["role"] == "user"]
        self.assertTrue(asked[1].startswith("[Present: Mira Oakhand]\nI vanish the cart."))
        self.assertTrue(asked[2].startswith("[Present: Mira Oakhand, Tobin]\n"))
        self.assertTrue(asked[3].startswith("[Present: Mira Oakhand, Tobin]\n"))                    # worked out from who spoke, for a turn that did not record it
        self.assertTrue(asked[4].startswith("Old turn."))                                           # nothing is claimed about a turn nobody kept track of
        self.assertIn("[What each character knows]", system)
        del self.state["history"][-1]
        last = prompt.narrator_prompt(self.card, self.state, self.preset, "I ask Tobin what he saw.", [])[1][-1]["content"]
        self.assertIn("Here only since recently: Tobin (the last 2 turns).", last)
        self.assertNotIn("Mira Oakhand (the last", last)                                             # she has been there throughout
        self.assertIn("Present: Mira Oakhand\nPlayer: I vanish the cart.", prompt.summary_prompt(self.card, self.state, self.state["history"])[1][0]["content"])

    def test_the_world_moves_on_while_the_player_is_elsewhere(self):
        """Asked for after play: a captain last seen at the exam was still "at the coliseum" many scenes later."""
        self.state["actors"]["marsh_bandit"]["known"] = False
        self.assertEqual(prompt.offstage(self.card, self.state), ["tobin"])                          # Mira is with the player; the bandit is not someone they know
        user = prompt.world_prompt(self.card, self.state)[1][0]["content"]
        self.assertIn("- Tobin (id: tobin).", user)
        self.assertNotIn("Mira Oakhand (id:", user)
        here = self.state["actors"]["player"]["location"]
        said = json.dumps({"whereabouts": [{"id": "tobin", "location": "cellar", "note": "fetching a cask"}, {"id": "tobin", "location": here}, {"id": "tobin", "location": "here"},
                                           {"id": "mira", "location": "stable"}, {"id": "marsh_bandit", "location": "stable"}]})
        moves = prompt.parse_world(said, self.card, self.state)
        self.assertEqual(moves, [{"id": "tobin", "location": "cellar", "note": "fetching a cask"}])    # never to the player, never someone who is with them or unknown to them
        track(self.card, self.state, moves)
        self.assertEqual((self.state["actors"]["tobin"]["location"], self.state["actors"]["tobin"]["note"]), ("cellar", "fetching a cask"))
        gone = prompt.parse_world(json.dumps({"whereabouts": [{"id": "tobin", "location": None, "note": "left for the coast"}]}), self.card, self.state)
        track(self.card, self.state, gone)
        self.assertIsNone(self.state["actors"]["tobin"]["location"])                                  # business over: off the map, not left standing where he was

    def test_the_director_cannot_move_someone_the_text_knows_nothing_of(self):
        """Seen in play: the player walked to the stable, the text never mentioned Mira, and the
        director reported her gone from the map."""
        apply_actions(self.card, self.state, [{"type": "move", "location": "stable"}], by_player=True)
        text = "You duck into the stable. Tobin looks up from the stall."
        updates = [{"id": "tobin", "location": None, "note": "at the stall"}, {"id": "marsh_bandit", "location": "here"}, {"id": "mira", "location": None}, {"id": "mira", "note": "wiping the bar"}]
        kept = prompt.credible_whereabouts(self.card, self.state, text, updates)
        self.assertEqual([(u["id"], u.get("location", "-")) for u in kept], [("tobin", None), ("marsh_bandit", "here"), ("mira", "-")])
        kept = prompt.credible_whereabouts(self.card, self.state, "Word comes that Mira Oakhand has left for the capital.", [{"id": "mira", "location": None}])
        self.assertEqual(len(kept), 1)                                                # named in the text: believed

    def test_an_unreadable_answer_is_not_taken_for_nothing_to_report(self):
        """A helper that answers in prose, or with the wrong shape, has failed. Reading that as an
        empty list let turns go through with their changes unrecorded."""
        self.assertTrue(prompt.answers_with('{"actions": []}', "actions"))
        self.assertTrue(prompt.answers_with('```json\n{"actions": [{"type": "move"}]}\n```', "actions"))
        for bad in ("I'm sorry, I can't do that.", "", '{"actions": "none"}', '{"verdicts": []}', "[]", '{"actions"'):
            self.assertFalse(prompt.answers_with(bad, "actions"), bad)
        self.assertEqual(prompt.parse_bookkeeper("I'm sorry, I can't do that."), [])       # which is why the check has to come first

    def test_the_models_are_told_what_a_state_is(self):
        """A state is a lasting condition of body, situation or mind, not a single act. Both the
        story model and the bookkeeper get the same explanation, with the test for telling them apart."""
        for record in (True, False):
            told = prompt.states_reference(self.card, record)
            for part in ("true of a character for a while", "of the body (asleep", "of their situation (tied up, kidnapped", "of the mind (grieving",
                         "What someone does in a moment is not a state", "would still be true several turns from now", "Any other lasting condition can be a state"):
                self.assertIn(part, told)
        system = prompt.bookkeeper_prompt(self.card, self.state, "I wait.", [], "Rain.")[0]
        self.assertIn("never kept with a note saying it is over", system)
        self.assertIn("A single act is not recorded", system)
        self.assertLess(system.index("- States, as [States] below"), system.index("[States]\nA state is something"))

    def test_bookkeeper_is_given_everything_it_needs(self):
        apply_actions(self.card, self.state, [{"type": "set_state", "state": "airborne", "note": "on a broom"}])
        results = [{"ok": True, "message": "Traveler uses Quick Strike on Mira Oakhand."}]
        system, messages = prompt.bookkeeper_prompt(self.card, self.state, "I land and cast a light.", results,
                                                    "You touch down in the yard. A cold flicker of marsh-light leaves your fingers.")
        for part in ["bookkeeper", '"type": "change_stat"', '"type": "clear_state"', '"type": "move"', "Costs and harm", "go through every state the game state lists",
                     "the player ends the text somewhere other", "Do not record them again", "Restrained (restrained)", 'Reply with JSON only: {"actions": [ ... ]}']:
            self.assertIn(part, system, part)
        self.assertNotIn("{{user}}", system)
        user = messages[0]["content"]
        for part in ["State: Airborne (on a broom)", "Mana (mana) 10/10", "[Player's message]\nI land and cast a light.",
                     "[Engine results already recorded this turn]\n- done: Traveler uses Quick Strike", "[Narrator's new text]\nYou touch down", "[Active quests]"]:
            self.assertIn(part, user, part)

    def test_what_it_reports_is_applied_and_checked(self):
        apply_actions(self.card, self.state, [{"type": "set_state", "state": "airborne", "note": "on a broom"}])
        reply = """Here is what changed:
```json
{"actions": [{"type": "change_stat", "who": "player", "stat": "mana", "amount": -4},
             {"type": "clear_state", "who": "player", "state": "airborne"},
             {"type": "move", "who": "player", "location": "stable"},
             {"type": "add_item", "who": "player", "item": "dragon_egg"}, "nonsense"]}
```"""
        actions = prompt.parse_bookkeeper(reply)
        self.assertEqual(len(actions), 4)
        results = apply_actions(self.card, self.state, actions)
        self.assertEqual([r["ok"] for r in results], [True, True, True, False])       # the invented item is refused, as from any source
        me = self.state["actors"]["player"]
        self.assertEqual((me["stats"]["mana"], me["states"], me["location"]), (6, {}, "stable"))
        self.assertEqual(prompt.parse_bookkeeper("I could not tell."), [])
        self.assertEqual(prompt.parse_bookkeeper('{"actions": []}'), [])

    def test_only_asked_about_what_the_card_tracks(self):
        cafe = load_card(os.path.join(ROOT, "cards", "quiet_cafe"))
        system, messages = prompt.bookkeeper_prompt(cafe, new_game(cafe), "Hello.", [], "Noor smiles.")
        self.assertIn("Relationships", system)
        for absent in ["Costs and harm", "Belongings", "Experience", "Fights", "change_money", "add_item"]:
            self.assertNotIn(absent, system, absent)


class QuestJudgeTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        self.quest = self.state["quests"]["missing_courier"]

    def test_a_quest_moves_one_objective_at_a_time(self):
        thrice = [{"type": "quest_advance", "quest": "missing_courier"}] * 3
        results = apply_actions(self.card, self.state, thrice)
        self.assertEqual(len(results), 1)                                   # the repeats are dropped, not shown as errors
        self.assertEqual(self.quest, {"status": "active", "stage": 1, "met": []})
        del self.card.quests["missing_courier"]["stages"][1]["done_if"]         # leave this one to the story, as before
        apply_actions(self.card, self.state, [{"type": "quest_advance", "quest": "Missing_Courier"}, {"type": "quest_advance", "quest": "missing_courier"}])
        self.assertEqual(self.quest["stage"], 2)

    def test_result_lines_a_model_made_up_are_not_story(self):
        story, actions = prompt.parse_narration("[Engine results]\n- done: Ash leaves Stable and goes to Common Room.\n\nYou step back into the warmth.\n- She nods.")
        self.assertEqual(story, "You step back into the warmth.\n- She nods.")

    def test_the_narrator_is_briefed_on_who_is_in_the_scene(self):
        apply_actions(self.card, self.state, [{"type": "move", "location": "stable"}], by_player=True)
        track(self.card, self.state, [{"id": "tobin", "location": "here", "note": "hiding a letter"}, {"id": "mira", "note": "behind the bar"}])
        self.state["scene"] = prompt.parse_scene('{"scene": "Tobin is  hiding a letter\\nfrom Traveler."}')
        brief = prompt.describe_scene(self.card, self.state)
        self.assertIn("Place: Stable.\nWith Traveler: Tobin (hiding a letter).", brief)
        self.assertNotIn("Mira", brief)                                           # who is somewhere else is not named: a model uses whoever it is shown
        self.assertNotIn("Bandit", brief)
        self.assertIn("What is going on: Tobin is hiding a letter from Traveler.", brief)
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            preset = json.load(f)
        system, messages = prompt.narrator_prompt(self.card, self.state, preset, "I wait.", [])
        self.assertIn("[The scene right now]", messages[-1]["content"])          # in the part that changes, not the cached part
        self.assertNotIn("[The scene right now]", system)
        apply_actions(self.card, self.state, [{"type": "move", "location": "common_room"}], by_player=True)
        self.assertNotIn("What is going on", prompt.describe_scene(self.card, self.state))   # a new place, a new scene
        self.assertEqual(prompt.parse_scene("not json"), "")

    def test_only_who_has_entered_the_story_is_described(self):
        card = self.card
        self.assertEqual(self.state["cast"], ["mira", "tobin", "marsh_bandit"])   # Mira is in the room; the first objective names Tobin, the scenario the bandit
        del card.quests["missing_courier"]["stages"][0]["done_when"]              # a card whose own direction does not name Tobin
        state = new_game(card)
        with open(os.path.join(ROOT, "presets", "default.preset.json")) as f:
            preset = json.load(f)
        self.assertEqual(state["cast"], ["mira", "marsh_bandit"])
        system, messages = prompt.narrator_prompt(card, state, preset, "I look around.", [], record=False)
        everything = system + "\n".join(m["content"] for m in messages)
        for unseen in ("Tobin", "Stable boy", "sealed_letter"):
            self.assertNotIn(unseen, everything)
        self.assertNotIn("All items that exist", everything)                      # with a bookkeeper, the story model needs no item ids
        self.assertIn("All items that exist", prompt.bookkeeper_prompt(card, state, "I wait.", [], "Rain.")[1][0]["content"])

        # Naming someone brings them in, in that turn's message, and the message stays as it was sent.
        self.assertEqual(prompt.arrivals(card, state, "I ask Mira where Tobin is."), ["tobin"])
        self.assertEqual(prompt.arrivals(card, state, "I walk through the marsh, thinking of a bandit."), [])   # a word is not a name
        system_2, messages_2 = prompt.narrator_prompt(card, state, preset, "I ask Mira where Tobin is.", [], record=False)
        self.assertEqual(system_2, system)                                        # the cached start of the prompt did not move
        self.assertIn("[Entering the story]\nTobin: Stable boy", messages_2[-1]["content"])
        self.assertIn("Elsewhere: Tobin (tobin) at Stable (stable)", messages_2[-1]["content"])     # now that he is in the story, where he is matters
        state["history"].append({"player": "I ask Mira where Tobin is.", "results": [], "narration": "\"Stable,\" she says.", "cast": ["tobin"]})
        state["cast"].append("tobin")
        system_3, messages_3 = prompt.narrator_prompt(card, state, preset, "I nod.", [], record=False)
        self.assertEqual(system_3, system)
        self.assertTrue(messages_3[2]["content"].startswith("[Entering the story]\nTobin: Stable boy"))
        self.assertNotIn("[Entering the story]", messages_3[-1]["content"])
        state["summarized"] = 1                                                   # that turn is folded into the summary: he joins the rest
        self.assertIn("Tobin: Stable boy", prompt.narrator_prompt(card, state, preset, "I nod.", [], record=False)[0])

    def test_someone_in_the_scene_but_not_in_the_exchange_gets_one_line(self):
        card, state = self.card, self.state
        apply_actions(card, state, [{"type": "move", "who": "tobin", "location": "common_room"}])
        both = prompt.describe_state(card, state)                                 # asked for everything
        self.assertEqual(both.count("Inventory:"), 3)
        about_mira = prompt.describe_state(card, state, focus="I ask Mira for stew.")
        self.assertIn("- Mira Oakhand (mira): Level 1", about_mira)
        self.assertIn("- Tobin (tobin): Health 10/20; Trust toward the player: 40/100", about_mira)
        self.assertEqual(about_mira.count("Inventory:"), 2)                       # the player's and Mira's

    def test_a_quest_is_only_offered_where_it_can_begin(self):
        from aigame.state import available_quests
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        card.data["quests"] += [{"id": "rats", "title": "Rats", "description": "Clear the cellar.", "giver": "mira", "stages": [{"id": "a", "description": "Kill rats."}]},
                                {"id": "foal", "title": "The Foal", "description": "Help Tobin.", "start_location": "stable", "stages": [{"id": "a", "description": "Help."}]},
                                {"id": "reward", "title": "A Favour Owed", "description": "Mira owes you.", "after": "missing_courier", "stages": [{"id": "a", "description": "Collect."}]}]
        card = Card(card.data, card.path)
        state = new_game(card)
        self.assertEqual([q["id"] for q in available_quests(card, state)], ["rats"])          # Mira is here; the stable is not; the courier is not found
        self.assertIn("[Quests that can begin now]\n- Rats (rats)", prompt.describe_quests(card, state))
        self.assertNotIn("Foal", prompt.describe_quests(card, state) + prompt.judge_prompt(card, state, "hi", "Rain.")[1][0]["content"])
        apply_actions(card, state, [{"type": "move", "location": "stable"}], by_player=True)
        self.assertEqual([q["id"] for q in available_quests(card, state)], ["foal"])
        from aigame.card import check_card
        bad = json.loads(json.dumps(card.data))
        bad["quests"][1].update(giver="nobody", start_location="moon", after="rats")
        bad["quests"][3].update(after="nothing", auto_start=True)
        problems = "\n".join(check_card(bad))
        for expected in ("given by unknown character 'nobody'", "starts at unknown location 'moon'", "comes after unknown quest 'rats'", "comes after unknown quest 'nothing'", "cannot both be active"):
            self.assertIn(expected, problems)
        early = apply_actions(card, state, [{"type": "quest_start", "quest": "reward"}])[0]
        self.assertEqual((early["ok"], early["message"]), (False, "Quest A Favour Owed cannot begin until The Missing Courier is finished."))
        state["quests"]["missing_courier"]["status"] = "done"
        self.assertEqual([q["id"] for q in available_quests(card, state)], ["foal", "reward"])

    def test_a_save_from_before_the_cast_was_tracked(self):
        from aigame.state import reconcile
        old = dict(self.state, history=[{"player": "I find Tobin.", "results": [], "narration": "He is in the stable."}])
        del old["cast"]
        reconcile(self.card, old)
        self.assertEqual(old["cast"], ["mira", "tobin", "marsh_bandit"])
        del self.card.quests["missing_courier"]["stages"][0]["done_when"]
        older = dict(new_game(self.card), history=[{"player": "I find Tobin.", "results": [], "narration": "He is in the stable."}])
        del older["cast"]
        reconcile(self.card, older)
        self.assertEqual(older["cast"], ["mira", "marsh_bandit", "tobin"])        # named in the story so far, so he has entered it

    def test_the_judge_is_told_which_objectives_are_the_games(self):
        self.quest["stage"] = 1
        told = prompt.judge_prompt(self.card, self.state, "I look around.", "Rain.")[1][0]["content"]
        self.assertIn("The game itself marks this objective finished.", told)
        self.assertNotIn("Finished only when", told)

    def test_the_objective_named_must_be_the_one_in_progress(self):
        wrong = apply_actions(self.card, self.state, [{"type": "quest_advance", "quest": "missing_courier", "stage": "deliver"}])[0]
        self.assertFalse(wrong["ok"])
        self.assertIn("Its current objective is ask_around", wrong["message"])
        self.assertEqual(self.quest["stage"], 0)
        self.assertTrue(apply_actions(self.card, self.state, [{"type": "quest_advance", "quest": "missing_courier", "stage": "ask_around"}])[0]["ok"])
        self.assertEqual(self.quest["stage"], 1)

    def test_direction_and_finishing_condition_are_separate(self):
        stage = self.card.quests["missing_courier"]["stages"][0]
        stage["hint"] = "Tobin is nervous. Let the mare be seen in the stable."          # an older card: direction under the old name
        text = prompt.describe_quests(self.card, self.state)
        self.assertIn("Current objective [ask_around], part 1 of 3: Find out what happened to the courier.", text)
        self.assertIn("How to play it: Tobin is nervous.", text)
        self.assertIn("It is finished only when: the player learns the horse came back riderless", text)
        seen_by_player_helpers = prompt.describe_quests(self.card, self.state, "player")
        self.assertNotIn("Tobin is nervous", seen_by_player_helpers)                      # no spoilers in suggested replies
        self.assertNotIn("finished only when", seen_by_player_helpers)

    def test_judge_sees_the_objective_and_the_recent_story(self):
        self.card.quests["missing_courier"]["fail_when"] = "the player leaves the inn for good"
        self.card.quests["missing_courier"]["stages"][0]["hint"] = "Tobin is nervous."
        self.state["history"].append({"player": "I look around.", "results": [], "narration": "Rain on the shutters."})
        system, messages = prompt.judge_prompt(self.card, self.state, "I ask Tobin.", "Tobin swallows. \"The mare came back alone.\"")
        user = messages[0]["content"]
        for part in ["Current objective [ask_around], part 1 of 3", "Finished only when: the player learns the horse came back riderless",
                     "Fails if: the player leaves the inn for good", "Narrator: Rain on the shutters.", "[Newest]\nPlayer: I ask Tobin.", "The mare came back alone."]:
            self.assertIn(part, user, part)
        self.assertNotIn("Tobin is nervous", user)                                        # direction is not a condition, so the judge never sees it
        self.assertNotIn("find_letter", user)                                             # nor later objectives
        for part in ['"not_yet"', "When unsure, answer not_yet", "completely finished", "Give the reason before the verdict"]:
            self.assertIn(part, system, part)
        del self.card.quests["missing_courier"]["stages"][0]["done_when"]
        self.assertIn("Finished only when: everything the objective describes has happened and is over", prompt.judge_prompt(self.card, self.state, "x", "y")[1][0]["content"])

    def test_verdicts_become_checked_actions(self):
        reply = json.dumps({"verdicts": [
            {"quest": "missing_courier", "objective": "ask_around", "why": "Tobin said the mare came back alone.", "verdict": "done"},
            {"quest": "missing_courier", "objective": "find_letter", "why": "and the rest too", "verdict": "done"},
            {"quest": "made_up", "objective": "x", "verdict": "done"}, {"quest": "missing_courier", "verdict": "done"}, "junk"], "start": ["made_up"]})
        actions = prompt.parse_judge(reply, self.card)
        self.assertEqual(actions, [{"type": "quest_advance", "quest": "missing_courier", "stage": "ask_around"},
                                   {"type": "quest_advance", "quest": "missing_courier", "stage": "find_letter"}])
        apply_actions(self.card, self.state, actions)
        self.assertEqual(self.quest, {"status": "active", "stage": 1, "met": []})         # one step, however eager the judge
        self.assertEqual(prompt.parse_judge('{"verdicts": [{"quest": "missing_courier", "objective": "find_letter", "verdict": "not_yet"}]}', self.card), [])
        self.assertEqual(prompt.parse_judge('{"verdicts": [{"quest": "missing_courier", "verdict": "failed"}]}', self.card), [{"type": "quest_fail", "quest": "missing_courier"}])
        self.assertEqual(prompt.parse_judge("no idea", self.card), [])

    def test_bookkeeper_leaves_quests_to_the_judge(self):
        system, messages = prompt.bookkeeper_prompt(self.card, self.state, "Hi.", [], "Mira nods.", quests=False)
        self.assertNotIn("quest_advance", system)
        self.assertNotIn("Quests.", system)
        self.assertNotIn("[Active quests]", messages[0]["content"])
        self.assertIn("quest_advance", prompt.bookkeeper_prompt(self.card, self.state, "Hi.", [], "Mira nods.")[0])


class StoryMadePlacesTest(unittest.TestCase):
    def setUp(self):
        self.card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        self.state = new_game(self.card)
        self.me = self.state["actors"]["player"]

    def story(self, *actions):
        return apply_actions(self.card, self.state, list(actions))

    def test_the_story_can_send_people_somewhere_new(self):
        results = self.story({"type": "create_location", "name": "The Demon God's Void", "description": "Starless dark and a floor like glass.", "temporary": True},
                             {"type": "move", "who": "player", "location": "The Demon God's Void"},
                             {"type": "move", "who": "mira", "location": "gen_the_demon_god_s_void"})
        self.assertEqual([r["message"] for r in results], ["A new place: The Demon God's Void, for as long as someone is there.",
                                                           "Traveler leaves Common Room and goes to The Demon God's Void.",
                                                           "Mira Oakhand leaves Common Room and goes to The Demon God's Void."])
        self.assertEqual(self.me["location"], "gen_the_demon_god_s_void")
        self.assertIn("gen_the_demon_god_s_void", self.state["revealed"])
        text = prompt.describe_state(self.card, self.state)
        for part in ["Location: The Demon God's Void (gen_the_demon_god_s_void) [made by the story; temporary]. Exits on foot: none",
                     "Other places on the map, not reachable on foot from here: Common Room (common_room), Stable (stable), Cellar (cellar)",
                     "Characters here:\n- Mira Oakhand (mira)"]:
            self.assertIn(part, text, part)
        walk = apply_actions(self.card, self.state, [{"type": "move", "location": "common_room"}], by_player=True)[0]
        self.assertIsNone(walk["ok"])                                          # no way out on foot: it is up to the story
        self.assertEqual(pickle_roundtrip(self.state), self.state)

    def test_a_temporary_place_goes_when_the_last_person_leaves(self):
        self.story({"type": "create_location", "name": "Dream", "temporary": True}, {"type": "move", "who": "player", "location": "Dream"}, {"type": "move", "who": "mira", "location": "Dream"})
        self.story({"type": "move", "who": "player", "location": "common_room"})
        self.assertIn("gen_dream", self.state["generated_locations"])          # Mira is still in it
        from aigame.state import track
        track(self.card, self.state, [{"id": "mira", "location": "here"}])     # the scene director notices she came back too
        self.assertEqual(self.state["generated_locations"], {})
        self.assertNotIn("gen_dream", self.state["revealed"])
        self.assertFalse(self.story({"type": "move", "who": "tobin", "location": "Dream"})[0]["ok"])

    def test_a_lasting_place_stays_and_can_be_joined_to_the_map(self):
        self.story({"type": "create_location", "name": "Roadside Camp", "connected_to": "stable"}, {"type": "move", "who": "player", "location": "Roadside Camp"})
        self.story({"type": "move", "who": "player", "location": "stable"})
        self.assertIn("gen_roadside_camp", self.state["generated_locations"])
        self.assertIn("Roadside Camp (gen_roadside_camp)", prompt.describe_state(self.card, self.state).split("Exits on foot:")[1].split("\n")[0])
        self.assertTrue(apply_actions(self.card, self.state, [{"type": "move", "location": "gen_roadside_camp"}], by_player=True)[0]["ok"])
        again = self.story({"type": "create_location", "name": "roadside camp"})[0]
        self.assertEqual((again["ok"], len(self.state["generated_locations"])), (True, 1))      # asking twice does not make two
        self.assertFalse(self.story({"type": "create_location", "name": ""})[0]["ok"])
        self.assertFalse(self.story({"type": "create_location", "name": "X", "connected_to": "atlantis"})[0]["ok"])

    def test_helpers_know_about_made_places(self):
        self.story({"type": "create_location", "name": "Roadside Camp"})
        self.assertIn("Roadside Camp (gen_roadside_camp)", prompt.director_prompt(self.card, self.state, ["x"])[1][0]["content"])
        self.assertEqual(prompt.parse_whereabouts('{"whereabouts": [{"id": "tobin", "location": "gen_roadside_camp"}]}', self.card, self.state), [{"id": "tobin", "location": "gen_roadside_camp"}])
        self.assertEqual(prompt.parse_whereabouts('{"whereabouts": [{"id": "tobin", "location": "gen_roadside_camp"}]}', self.card), [])
        self.assertIn("create_location", prompt.action_protocol(self.card))
        self.assertIn("create_location it first", prompt.bookkeeper_prompt(self.card, self.state, "x", [], "y")[0])

    def test_a_card_can_keep_its_map_fixed(self):
        self.card.data["rules"]["allow_generated_locations"] = False
        refused = self.story({"type": "create_location", "name": "Void"})[0]
        self.assertEqual((refused["ok"], refused["message"]), (False, "This game's map is fixed; the story cannot add places to it."))
        self.assertNotIn("create_location", prompt.action_protocol(self.card))
        self.assertNotIn('"type": "create_location"', prompt.bookkeeper_prompt(self.card, self.state, "x", [], "y")[0])

    def test_old_saves_gain_the_list(self):
        from aigame.state import reconcile
        del self.state["generated_locations"]
        reconcile(self.card, self.state)
        self.assertEqual(self.state["generated_locations"], {})


def pickle_roundtrip(value):
    import pickle
    return pickle.loads(pickle.dumps(value))


if __name__ == "__main__":
    unittest.main()
