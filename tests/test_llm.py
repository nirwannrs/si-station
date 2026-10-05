"""Run with: python3 -m unittest discover tests"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "game"))

from aigame import llm, prompt  # noqa: E402
from aigame.actions import apply_actions  # noqa: E402
from aigame.card import load_card  # noqa: E402
from aigame.state import new_game  # noqa: E402

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

    def test_model_list_and_errors(self):
        self.assertEqual(llm.model_ids({"data": [{"id": "b"}, {"id": "a"}, {"x": 1}]}), ["a", "b"])
        self.assertIn("API key was not accepted", llm.describe_http_error(401, '{"error": {"message": "bad key"}}'))
        self.assertIn("bad key", llm.describe_http_error(401, '{"error": {"message": "bad key"}}'))
        self.assertIn("Could not reach", llm.describe_http_error(None, None))

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
        self.assertIn("Never decide Ash's actions", system)       # {{user}} filled in
        self.assertIn("Write in second person", system)            # enabled toggle
        self.assertNotIn("one or two short paragraphs", system)    # disabled toggle
        self.assertIn("Low fantasy", system)
        self.assertIn("Mira Oakhand: Innkeeper", system)           # the whole cast, the same every turn
        self.assertIn("Tobin: Stable boy", system)
        self.assertIn("<actions>", system)                         # the mechanics text is long and never changes
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual([bool(m.get("cache")) for m in messages], [False, False, False, True, False])
        self.assertIn("Rain follows you", messages[1]["content"])
        last = messages[-1]["content"]
        for part in ["[Current game state]", "[Active quests]", "Met when:", "I eat the stew", "- REJECTED: Ash does not have Bowl of Stew.",
                     "smuggler's cache"]:                           # lorebook entry triggered by "cellar"
            self.assertIn(part, last)
        self.assertNotIn("the lights", system + last)              # untriggered entry stays out
        for changing in ["[Current game state]", "[Active quests]", "[Background knowledge]"]:
            self.assertNotIn(changing, system)
        self.assertNotIn("{{user}}", system + last)

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
        self.assertIn("Mira Oakhand (id: mira). Expressions: angry, happy, neutral, worried", messages[0]["content"])
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
        for part in ["set_state", "clear_state", "[States]", "Restrained (restrained)", "stops them doing anything", "they are out of the scene"]:
            self.assertIn(part, protocol)
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
        self.assertIn("(Fails if: the player leaves the inn for good)", prompt.describe_quests(card, state))
        protocol = prompt.action_protocol(card)
        for part in ["[When {{user}} leaves a place]", "put them back with a move action", "quest_fail"]:
            self.assertIn(part, protocol)
        # the narrator sending them back is an ordinary move, and it is allowed
        back = apply_actions(card, state, prompt.parse_narration('A hand lands on your shoulder.\n<actions>{"actions": [{"type": "move", "who": "player", "location": "common_room"}]}</actions>')[1])
        self.assertTrue(back[0]["ok"])
        self.assertEqual(state["actors"]["player"]["location"], "common_room")

    def test_narrator_can_close_the_map(self):
        card = load_card(os.path.join(ROOT, "cards", "rusty_lantern"))
        state = new_game(card)
        self.assertEqual(apply_actions(card, state, [{"type": "lock_travel", "reason": "The exam has begun and the captains are watching."}])[0]["message"],
                         "Traveler can no longer leave: The exam has begun and the captains are watching.")
        tried = apply_actions(card, state, [{"type": "move", "location": "stable"}], by_player=True)[0]
        self.assertEqual((tried["ok"], tried["message"]), (False, "Traveler cannot leave right now: The exam has begun and the captains are watching."))
        self.assertEqual(state["actors"]["player"]["location"], "common_room")
        self.assertIn("Travel: LOCKED for the player (The exam has begun and the captains are watching.)", prompt.describe_state(card, state))
        self.assertTrue(apply_actions(card, state, [{"type": "move", "who": "player", "location": "cellar"}])[0]["ok"])     # the story can still move them
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
        results = apply_actions(self.card, self.state, list(actions))
        self.state["history"].append({"player": text, "results": [{"ok": r["ok"], "message": r["message"]} for r in results], "narration": narration})
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
        self.assertIn("Mira Oakhand (mira) [the player has not met them yet]", prompt.describe_state(card, state))
        meet(state, ["mira", "nobody"])                                                           # she speaks in a scene
        self.track(card, state, [{"id": "tobin", "location": "here"}, {"id": "marsh_bandit", "note": "watching from the road"}])
        self.assertEqual(known(), ["mira", "tobin"])                                              # the bandit was only mentioned at a distance
        self.assertNotIn("Mira Oakhand (mira) [the player has not met them yet]", prompt.describe_state(card, state))
        self.assertTrue(all(a["known"] for a in new_game(self.card)["actors"].values()))          # the default is that everyone is known

    def test_old_saves_gain_the_note(self):
        from aigame.state import reconcile
        for actor in self.actors.values():
            del actor["note"]
        reconcile(self.card, self.state)
        self.assertEqual(self.actors["mira"]["note"], "")


if __name__ == "__main__":
    unittest.main()
