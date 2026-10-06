## A self-test: the game plays through what a player does, by itself, and writes down what worked.
##
## It exists because the parts were each tested alone and a release still broke where they meet:
## a save made while a card was a folder could not be loaded once the card had been imported as a
## file. Tests of single functions cannot catch that; only doing the whole thing can. This does
## the whole thing, inside the real game, and so also inside a built download.
##
## It runs only when the game is started with SI_STATION_SELFTEST set to the path of a report file
## (tools/release_check.sh does that). It talks to a pretend model that it starts itself, on this
## computer, so it needs no key and costs nothing. Run it with --savedir and SI_STATION_DATA
## pointing at scratch folders: it imports, saves and deletes things.

init -5 python:
    import os

    def selftest_report_path():
        return os.environ.get("SI_STATION_SELFTEST") or ""

    if selftest_report_path():
        config.save_persistent = False
        config.label_overrides["splashscreen"] = "selftest"

init python:

    class SelfTest(object):
        steps = []
        slot = ""
        stored = ""
        garbled = set()         # kinds of request the pretend model answers uselessly, to test failures
        asked = []              # every kind of request it has had

    selftest = SelfTest()

    def selftest_write(finished):
        import json
        failed = [name for name, ok, detail in selftest.steps if not ok]
        with open(selftest_report_path(), "w") as f:
            json.dump({"finished": finished, "ok": finished and not failed, "failed": failed,
                       "steps": [{"step": name, "ok": ok, "detail": detail} for name, ok, detail in selftest.steps]}, f, indent=1)

    def selftest_step(name, work):
        """Runs one step. work returns a short description of what it found, or raises."""
        import traceback
        try:
            detail = work()
            selftest.steps.append((name, True, str(detail if detail is not None else "")))
        except Exception:
            selftest.steps.append((name, False, traceback.format_exc()[-900:]))
        selftest_write(False)

    def selftest_model():
        """A stand-in for a model provider, answering each kind of request the game makes. Returns its address."""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Pretend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                asked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                system = asked["messages"][0]["content"]
                kind = "bookkeeper" if "bookkeeper of" in system else "narrator" if "You are the narrator" in system else "other"
                selftest.asked.append(kind)
                if kind in selftest.garbled:
                    text = "I am sorry, I cannot produce that right now."
                elif "You are the narrator" in system:
                    text = "Rain drums on the roof.\n\n\"Stew is one gold,\" Mira says."
                elif "keep the journal" in system:
                    text = '{"title": "A quiet start", "content": "The player arrived at the inn.", "keywords": ["inn"]}'
                elif "running summary" in system:
                    text = "The player arrived at the inn."
                else:
                    text = '{"actions": [], "verdicts": [], "start": [], "paragraphs": [], "whereabouts": [], "revealed": [], "scene": "Quiet.", "choices": ["I wait."]}'
                body = json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Pretend)
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        return "http://127.0.0.1:%d/v1" % httpd.server_address[1]

    SELFTEST_CARD = "rusty_lantern"

    ## The test is spread over several statements of the label below, as play is: Ren'Py puts a
    ## loaded game back to how things stood at the start of the statement it was saved in, so the
    ## game must have been started in an earlier statement than the one that saves it.

    def selftest_play():
        sample = SELFTEST_CARD

        def data_folder():
            assert os.path.isfile(os.path.join(cards_dir(), sample, "card.json")), "the sample card is not in %s" % cards_dir()
            assert os.path.isfile(os.path.join(preset_folder(), "default.preset.json")), "the game's preset is not in %s" % preset_folder()
            return "cards in %s (%s)" % (cards_dir(), "the project itself" if config.developer else "the player's own folder")
        selftest_step("the data folder has the sample card and the preset", data_folder)

        def models():
            address = selftest_model()
            pretend = {"connections": [{"id": name, "provider": "local", "base_url": address, "api_key": ""} for name in ("main", "utility", "memory")],
                       "models": {"main": {"connection": "main", "model": "pretend"}, "utility": {"connection": "main", "model": "", "enabled": False},
                                  "memory": {"connection": "main", "model": ""}}, "tasks": {}}
            ## Settings are not written to disk during a self-test, so the player's own are untouched.
            persistent.llm = pretend
            persistent.preset, persistent.preset_file, persistent.params = None, None, None
            store.quick_menu = False
            return address
        selftest_step("a pretend model is listening", models)

        def play_as_folder():
            persistent.card = sample
            start_card(sample, chosen_persona())
            for text in ("I look around.", "I ask Mira about the courier."):
                open_turn()
                play_turn(text)
                assert turn_error is None, turn_error
            assert len(game_state["history"]) == 2
            return "2 turns, last reply: %r" % game_state["history"][-1]["narration"][:30]
        selftest_step("a game is played from a card that is a folder", play_as_folder)

        def undo():
            undo_turn()
            assert len(game_state["history"]) == 1, "undo left %d turns" % len(game_state["history"])
            open_turn()
            play_turn("I ask Mira about the courier.")
            assert turn_error is None and len(game_state["history"]) == 2
        selftest_step("a turn is taken back and played again", undo)

        def all_or_nothing():
            ## One of the helpers gives an answer the game cannot read. The turn must not go through
            ## half-done: nothing changes, the player is told, and sending again does not ask the
            ## story model a second time for a reply it already gave.
            import copy
            params()["bookkeeper"] = True
            before = copy.deepcopy(dict((k, v) for k, v in game_state.items() if k not in ("undo_stack", "open", "restore_point")))
            selftest.garbled.add("bookkeeper")
            del selftest.asked[:]
            open_turn()
            play_turn("I sit by the fire.")
            selftest.garbled.clear()
            after = dict((k, v) for k, v in game_state.items() if k not in ("undo_stack", "open", "restore_point"))
            assert turn_error, "a turn went through although the bookkeeper's answer was unreadable"
            assert after == before, "a failed turn changed: %s" % [k for k in before if after.get(k) != before[k]]
            assert selftest.asked.count("narrator") == 1
            open_turn()
            play_turn("I sit by the fire.")
            assert turn_error is None, turn_error
            assert selftest.asked.count("narrator") == 1, "the story model was asked again for a reply it had already given"
            undo_turn()
            return "nothing changed, then went through on the resend without a second story call"
        selftest_step("a turn whose helper fails changes nothing, and picks up where it failed", all_or_nothing)

        def resend_rule():
            ## A request is sent again by itself only when it cannot have been answered. A connection
            ## that drops after the wait must never be: that is the same reply written and paid for twice.
            import requests
            aborted = requests.exceptions.ConnectionError("('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))")
            for error, connected, lost in ((requests.exceptions.ConnectTimeout("x"), True, True), (requests.exceptions.SSLError("x"), True, True),
                                           (requests.exceptions.ConnectionError("Failed to establish a new connection"), True, True),
                                           (aborted, False, True), (requests.exceptions.ReadTimeout("x"), False, False), (ValueError("x"), False, False)):
                assert (never_connected(error), dropped(error)) == (connected, lost), repr(error)
            return "never connected: retried; dropped after a wait: not retried"
        selftest_step("a request is only sent again when it cannot have been answered", resend_rule)

        def journal():
            journal_now()
            assert len(game_state["journal"]) == 1, runtime.journal_message
            return game_state["journal"][0]["title"]
        selftest_step("a journal entry is written on request", journal)

        def stays_quick():
            ## A text-only card with a long story behind it: typing a letter and turning the mouse
            ## wheel must not make the game draw the whole story again. Both once took a quarter of a
            ## second, which nothing else here would have noticed, since nothing was wrong, only slow.
            import copy
            import time
            import pygame_sdl2
            card, real = current_card(), store.game_state
            shown = card.data.get("display")
            try:
                card.data["display"] = {"mode": "text"}
                store.game_state = copy.deepcopy(real)
                long_reply = "\n\n".join(["Mira wipes down the bar and eyes you over the rim of a tankard, saying nothing for a long while, and the rain keeps on."] * 6)
                for n in range(30):
                    store.game_state["history"].append({"player": "I wait, turn %d." % n, "results": [], "narration": long_reply})
                renpy.show_screen("story_panel")
                renpy.show_screen("turn_input")
                renpy.pause(1.0, hard=True, modal=False)
                assert renpy.get_screen("story_panel") is not None, "the story of a text-only card is not on screen"
                scroll = renpy.get_screen("story_panel").scope["story_scroll"]
                assert scroll.range > 1000, "the story cannot be scrolled (range %s)" % scroll.range

                def timed(events, times=10):
                    started = time.time()
                    for i in range(times):
                        for event in events:
                            pygame_sdl2.event.post(event)
                        renpy.pause(0.001, hard=True, modal=False)
                    return (time.time() - started) / times * 1000

                ## The wheel is given to the story view directly. Posting a mouse event to the window only
                ## works while the window has the mouse, which a test run in the background does not.
                view, at = StoryView.latest, scroll.value
                try:
                    view.event(pygame_sdl2.event.Event(pygame_sdl2.MOUSEBUTTONDOWN, button=4, pos=(900, 500), mod=0), 900, 300, 0)
                except renpy.IgnoreEvent:
                    pass
                assert scroll.value < at, "the mouse wheel did not move the story (at %s before, %s after, of %s)" % (at, scroll.value, scroll.range)

                def scrolled(times=10):
                    started = time.time()
                    for i in range(times):
                        scroll.change(max(0, scroll.value - 120))
                        renpy.pause(0.001, hard=True, modal=False)
                    return (time.time() - started) / times * 1000

                wheel = scrolled()
                key = timed([pygame_sdl2.event.Event(pygame_sdl2.TEXTINPUT, text="a")])
                assert renpy.get_screen("turn_input").scope["typed"].startswith("aaa"), "typing did not reach the input box"
                assert wheel < 100 and key < 100, "slow: %.0f ms for a wheel step, %.0f ms for a key (both should be well under 100)" % (wheel, key)
                assert StoryView.latest.showing(), "the story is not drawn"
                ## Opening the menu puts something else on screen for a while and then comes back to the
                ## very same story screen. The story must still be drawn then. (It once came back blank.)
                view = StoryView.latest
                renpy.call_in_new_context("selftest_elsewhere")
                renpy.pause(0.5, hard=True, modal=False)
                assert StoryView.latest is view, "the story screen was rebuilt, so this did not test coming back to it"
                assert view.showing(), "the story is blank after the menu has been open"
                return "%.0f ms for a wheel step, %.0f ms for a key, with %d turns behind" % (wheel, key, len(store.game_state["history"]))
            finally:
                renpy.hide_screen("turn_input")
                renpy.hide_screen("story_panel")
                store.game_state = real
                if shown is None:
                    card.data.pop("display", None)
                else:
                    card.data["display"] = shown
        selftest_step("typing and scrolling stay quick in a long text-only story", stays_quick)

    def selftest_save():
        def save():
            ## Named the way the save screen names a slot, so this is a save the player would see listed.
            selftest.slot = "%sselftest-1" % card_save_prefix()
            renpy.save(selftest.slot)
            assert renpy.can_load(selftest.slot), "the save was not written"
            return "%s, remembering its card as %r" % (selftest.slot, store.card_name)
        selftest_step("the game is saved", save)

    def selftest_around():
        import json
        import shutil
        import tempfile
        work = tempfile.mkdtemp()
        sample = SELFTEST_CARD

        def export_and_import():
            ## What a creator does to share a card, and a player to receive it: the folder becomes a
            ## .sicard file, and only the file is in the library afterwards.
            packed = aig_card.pack_card(os.path.join(cards_dir(), sample), os.path.join(work, sample + aig_card.EXTENSION))
            shutil.move(os.path.join(cards_dir(), sample), os.path.join(work, "folder_set_aside"))
            aig_card._cache.clear()
            selftest.stored = aig_card.import_card(packed, cards_dir())
            persistent.card = selftest.stored
            assert inserted_card() is not None and inserted_card().id == sample
            return "in the library as %r" % selftest.stored
        selftest_step("the card is exported and imported as a file", export_and_import)

        def creator_runs():
            import requests
            start_creator()
            listing = requests.get(creator.url + "/api/projects", timeout=10).json()
            page = requests.get(creator.url + "/", timeout=10)
            presets = requests.get(creator.url + "/api/presets", timeout=10).json()
            assert page.status_code == 200 and "app.js" in page.text, "the creator's page was not served"
            assert any(p["id"] == "default" for p in presets["presets"]), "the creator does not list the game's preset"
            assert len(presets["wording"]) >= 12, "the creator lists %d built-in prompts" % len(presets["wording"])
            return "%s, cards: %s" % (creator.url, sorted(p["id"] for p in listing["projects"]))
        selftest_step("the card creator starts from inside the game", creator_runs)

        def preset_round_trip():
            shared = os.path.join(work, "shared.preset.json")
            with open(os.path.join(preset_folder(), "default.preset.json")) as f:
                preset = json.load(f)
            preset["name"], preset["prompts"] = "Self-test preset", {"summarize": "Summarize in fifty words."}
            with open(shared, "w") as f:
                json.dump(preset, f)
            name = aig_wording.import_preset(shared, preset_folder())
            preset_choose(name)
            assert active_preset()["prompts"].get("summarize") == "Summarize in fifty words."
            preset_choose("default")
            os.remove(os.path.join(preset_folder(), name + ".preset.json"))
            return "imported as %r, used, removed" % name
        selftest_step("a shared preset is imported and used", preset_round_trip)

        def keys():
            name = "selftest:" + str(os.getpid())
            assert aig_keystore.put(name, "not-a-real-key-123"), "the key store refused"
            assert aig_keystore.get(name) == "not-a-real-key-123"
            aig_keystore.drop(name)
            assert aig_keystore.get(name) == ""
            return aig_keystore.kind()
        selftest_step("an API key is kept by the system and removed again", keys)

    def selftest_after_load(problem):
        """Called once the save made above has been loaded: the last step, and the one that failed in 1.0-beta.
        problem is what the game would have told the player instead of loading, if anything."""
        def loaded():
            assert problem is None, problem
            assert store.card_name == selftest.stored, "the save still points at %r, not %r" % (store.card_name, selftest.stored)
            assert len(game_state["history"]) == 2, "%d turns after loading" % len(game_state["history"])
            assert current_card().id == game_state["card_id"]
            return "plays on with %r, %d turns" % (store.card_name, len(game_state["history"]))
        selftest_step("the save loads although its card is now a file", loaded)
        selftest_write(True)
        renpy.quit(save=False)


## Stands in for the menu: another context, with none of the game's screens drawn, for half a second.
label selftest_elsewhere:
    scene black
    $ renpy.pause(0.5, hard=True)
    return


label selftest:
    $ selftest_play()
    $ open_turn()
    $ selftest_save()
    $ selftest_around()
    ## Loading starts the story again from the save, so nothing after this line runs: after_load takes over.
    if selftest.slot and renpy.can_load(selftest.slot):
        $ renpy.load(selftest.slot)
    $ selftest.steps.append(("the save loads although its card is now a file", False, "there was no save to load"))
    $ selftest_write(True)
    $ renpy.quit(save=False)
