## LLM settings, calls and the turn loop.

## Device-local model setup, shaped like spec/settings.schema.json. Holds API keys, so it lives in
## persistent data and never in saves.
default persistent.llm = None

## Set when the next turn's text describes something the game has already applied.
default skip_resolve = False
## The player's choice of what Enter does in the story input: False sends, True starts a new line.
default persistent.enter_newline = False
## Why the last turn failed, or None.
default turn_error = None
## What the player typed for a turn that failed, so they do not have to retype it.
default draft = ""
## Suggested replies for the current turn.
default suggestions = []
## State as it was before the turn in progress, and whether one is in progress.
default turn_backup = None
default turn_running = False
## The player folded the reply panel away to see the stage.
default recap_hidden = False
## Character id speaking right now, so the stage can dim everyone else. None between lines.
default stage_speaker = None

init python:
    import copy
    import json
    from aigame import llm as aig_llm
    from aigame import prompt as aig_prompt

    HELPER_SAMPLING = {"temperature": 0.2, "max_tokens": 1000}
    SUGGEST_SAMPLING = {"temperature": 0.8, "max_tokens": 1000}
    CHAT_TIMEOUT = 180
    ## Most recent turns listed in the History menu, and on the play screen of a text-only card.
    HISTORY_MENU_TURNS = 200
    TEXT_MODE_TURNS = 15

    def apply_enter_key():
        """Points Enter and Shift+Enter at sending or starting a new line, as the player chose."""
        plain, shifted = ["noshift_K_RETURN", "noshift_K_KP_ENTER"], ["shift_K_RETURN", "shift_K_KP_ENTER"]
        config.keymap["input_next_line"] = plain if persistent.enter_newline else shifted
        config.keymap["input_enter"] = shifted if persistent.enter_newline else plain
        renpy.clear_keymap_cache()

    def toggle_enter_key():
        persistent.enter_newline = not persistent.enter_newline
        apply_enter_key()
        renpy.restart_interaction()

    config.start_callbacks.append(apply_enter_key)

    class Runtime(object):
        """Per-session UI status. Never rebound after init, so it stays out of saves."""
        suggesting = False
        # slot -> list of model ids, or a status string
        models = {}
        # slot -> status string of the last connection test
        tests = {}
        # what the most recent calls cost, newest last: {"what", "input", "cached", "written", "output"}
        usage = []

    runtime = Runtime()

    ## Settings.

    def llm_settings():
        if persistent.llm is None:
            persistent.llm = {
                "connections": [
                    {"id": "main", "provider": "anthropic", "base_url": "", "api_key": ""},
                    {"id": "utility", "provider": "anthropic", "base_url": "", "api_key": ""},
                ],
                "models": {
                    "main": {"connection": "main", "model": ""},
                    "utility": {"connection": "main", "model": "", "enabled": False},
                },
                "tasks": {},
            }
        return persistent.llm

    def llm_connection(connection_id):
        for connection in llm_settings()["connections"]:
            if connection["id"] == connection_id:
                return connection

    def slot_connection(slot):
        return llm_connection(llm_settings()["models"][slot]["connection"])

    def llm_ready():
        return bool(llm_settings()["models"]["main"]["model"].strip())

    def slot_for(task):
        """The model slot a helper task runs on: utility unless it is off or the player moved the task to main."""
        settings = llm_settings()
        utility = settings["models"]["utility"]
        if utility.get("enabled") and utility["model"].strip() and settings["tasks"].get(task, "utility") == "utility":
            return "utility"
        return "main"

    input_fields = {}

    def settings_field(target, key):
        """One editable text field of the settings. Cached so a field keeps focus while the screen redraws."""
        if (id(target), key) not in input_fields:
            input_fields[id(target), key] = DictInputValue(target, key, default=False)
        return input_fields[id(target), key]

    ## Calls.

    def http_json(request, timeout, threaded=False):
        """Sends a request built by aig_llm and returns the decoded JSON body."""
        try:
            if threaded and not renpy.emscripten:
                # renpy.fetch pumps the display while it waits, which is only safe on the main thread.
                import requests
                response = requests.request("POST" if "json" in request else "GET", request["url"],
                                            json=request.get("json"), headers=request["headers"], timeout=timeout)
                if response.status_code >= 400:
                    raise aig_llm.LLMError(aig_llm.describe_http_error(response.status_code, response.text))
                return response.json()
            return renpy.fetch(request["url"], json=request.get("json"), headers=request["headers"], timeout=timeout, result="json")
        except renpy.FetchError as e:
            response = getattr(e.original_exception, "response", None)
            if response is not None:
                raise aig_llm.LLMError(aig_llm.describe_http_error(response.status_code, response.text))
            raise aig_llm.LLMError(aig_llm.describe_http_error(None, None) + " " + str(e))
        except aig_llm.LLMError:
            raise
        except Exception as e:
            raise aig_llm.LLMError(aig_llm.describe_http_error(None, None) + " " + str(e))

    def llm_call(slot, system, messages, sampling, threaded=False, what="story"):
        connection = slot_connection(slot)
        request = aig_llm.chat_request(connection, llm_settings()["models"][slot]["model"].strip(), system, messages, sampling)
        data = http_json(request, CHAT_TIMEOUT, threaded)
        runtime.usage = runtime.usage[-39:] + [dict(aig_llm.chat_usage(connection["provider"], data), what=what)]
        return aig_llm.chat_text(connection["provider"], data)

    def run_helper(task, prompt, sampling=HELPER_SAMPLING, threaded=False):
        """Runs one helper job on whichever model the player assigned to it. prompt is (system, messages)."""
        return llm_call(slot_for(task), prompt[0], prompt[1], sampling, threaded, what=task)

    def usage_lines():
        """Plain-language lines about what recent calls cost, for the Models screen."""
        def line(label, calls):
            sent, cached = sum(c["input"] for c in calls), sum(c["cached"] for c in calls)
            if not sent:
                return "%s: the provider did not report token counts." % label
            return "%s: %s tokens sent, %s of them (%d%%) read from cache at a reduced price; %s tokens in the replies." % (
                label, "{:,}".format(sent), "{:,}".format(cached), round(100.0 * cached / sent), "{:,}".format(sum(c["output"] for c in calls)))
        story = [c for c in runtime.usage if c["what"] == "story"]
        helpers = [c for c in runtime.usage if c["what"] not in ("story", "test")]
        lines = []
        if story:
            lines.append(line("Last story reply", story[-1:]))
            if len(story) > 1:
                lines.append(line("Last %d story replies" % len(story), story))
        if helpers:
            lines.append(line("Small tasks (%d calls)" % len(helpers), helpers))
        return lines

    def run_in_background(work, done):
        """Calls work() off the main thread, then done(result) on it. work must not touch the display."""
        def body():
            try:
                result = work()
            except Exception as e:
                result = e
            renpy.invoke_in_main_thread(done, result)
        renpy.invoke_in_thread(body)

    ## Preset.

    preset_cache = {}

    def active_preset():
        if "preset" not in preset_cache:
            with open(os.path.join(config.basedir, "presets", "default.preset.json"), "rb") as f:
                preset_cache["preset"] = json.loads(f.read().decode("utf-8"))
        return preset_cache["preset"]

    ## The turn.

    def play_turn(text, resolve=True):
        """One full turn: work out what the player attempts, apply it, narrate, apply what the narration changed.

        resolve is False when the game already knows what the player did (they pressed a button, or
        a fight just ended), so there is nothing to work out from the text.

        On failure the state is put back as it was and turn_error says why.
        """
        if store.turn_running:
            # Re-entered after loading a save made while this turn was waiting on the model.
            store.game_state = store.turn_backup
        store.turn_backup = copy.deepcopy(store.game_state)
        store.turn_running = True
        store.turn_error = None
        card, state, preset = current_card(), store.game_state, active_preset()
        try:
            ## A card with nothing the player can act on skips the call that works out their actions.
            attempts = []
            if resolve and aig_prompt.player_action_types(card):
                attempts = aig_prompt.parse_resolver(run_helper("resolve_actions", aig_prompt.resolver_prompt(card, state, text)), card)
            results = state["pending_results"] + aig_actions.apply_actions(card, state, attempts, by_player=True)

            system, messages = aig_prompt.narrator_prompt(card, state, preset, text, results)
            reply = llm_call("main", system, messages, preset.get("sampling"))
            narration, world_actions = aig_prompt.parse_narration(reply)
            if not narration:
                raise aig_llm.LLMError("The model returned no story text.")
            results = results + aig_actions.apply_actions(card, state, world_actions)
        except aig_llm.LLMError as e:
            store.game_state = store.turn_backup
            store.turn_error = str(e)
            store.draft = text
            store.turn_running = False
            return

        state["pending_results"] = []
        state["history"].append({"player": text, "results": [{"ok": r["ok"], "message": r["message"]} for r in results],
                                 "narration": narration, "direction": direct_scene(card, state, narration)})
        state["turn"] += 1
        store.game_log = (store.game_log + results)[-30:]
        store.draft = ""
        store.turn_running = False
        summarize_if_long(card, state, preset)

    def direct_scene(card, state, narration):
        """Works out who speaks in each paragraph and with what expression, and moves the characters
        to wherever the text left them. Plain narration and nobody moved if the model fails."""
        paragraphs = aig_prompt.split_paragraphs(narration)
        if not card.characters:
            return aig_prompt.parse_direction("", card, len(paragraphs))
        try:
            reply = run_helper("direct_scene", aig_prompt.director_prompt(card, state, paragraphs))
        except aig_llm.LLMError:
            reply = ""
        aig_state.track(card, state, aig_prompt.parse_whereabouts(reply, card))
        direction = aig_prompt.parse_direction(reply, card, len(paragraphs))
        ## Anyone who speaks in the scene is someone the player has now met.
        aig_state.meet(state, [d["speaker"] for d in direction if d["speaker"]])
        return direction

    def direct_opening():
        state = store.game_state
        state["opening_direction"] = direct_scene(current_card(), state, aig_prompt.opening(current_card(), state))

    def scene_lines(turn=None):
        """(speaker id or None, expression, paragraph) for a turn, or for the opening when turn is None."""
        card, state = current_card(), store.game_state
        text = turn["narration"] if turn else aig_prompt.opening(card, state)
        direction = (turn.get("direction") if turn else state.get("opening_direction")) or []
        lines = []
        for n, paragraph in enumerate(aig_prompt.split_paragraphs(text)):
            entry = direction[n] if n < len(direction) else {}
            lines.append((entry.get("speaker"), entry.get("expression"), paragraph))
        return lines

    def present(turn=None):
        """Plays a turn in the text box, one piece at a time, with the speaker's name and expression on stage."""
        card, state = current_card(), store.game_state
        for speaker, expression, paragraph in scene_lines(turn):
            store.stage_speaker = speaker
            if speaker and expression:
                state.setdefault("expressions", {})[speaker] = expression
            who = speaker_character(speaker) if speaker else narrator
            for piece in aig_prompt.split_for_display(paragraph):
                who(rich(piece))
        store.stage_speaker = None

    ## Stage: backgrounds, sprites and speaker names, all read from the card.

    stage_cache = {}

    def asset_image(card, path):
        """A displayable for an image inside a card, or None if it is missing or unreadable."""
        key = (card.path, path)
        if key not in stage_cache:
            try:
                stage_cache[key] = im.Data(card.read_asset(path), path)
            except Exception:
                stage_cache[key] = None
        return stage_cache[key]

    def card_image(path):
        return asset_image(current_card(), path)

    def speaker_color(char_id):
        return current_card().characters[char_id].get("color") or "#ffd28a"

    def speaker_character(char_id):
        key = (store.card_name, "who", char_id)
        if key not in stage_cache:
            stage_cache[key] = Character(esc(current_card().characters[char_id]["name"]), who_color=speaker_color(char_id))
        return stage_cache[key]

    def stage_background():
        here = current_card().locations.get(store.game_state["actors"][aig_card.PLAYER]["location"])
        return card_image(here["background"]) if here and here.get("background") else None

    def stage_cast():
        """What to draw for each character where the player is: (id, name, image or None, x position, is dimmed)."""
        card, state = current_card(), store.game_state
        here = state["actors"][aig_card.PLAYER]["location"]
        present = [c for c in card.data.get("characters", [])
                   if state["actors"][c["id"]]["location"] == here and not aig_state.is_away(card, state["actors"][c["id"]])]
        cast = []
        for n, c in enumerate(present):
            sprites = c.get("sprites") or {}
            wanted = state.get("expressions", {}).get(c["id"], "neutral")
            path = sprites.get(wanted) or sprites.get("neutral") or (sprites[sorted(sprites)[0]] if sprites else None)
            dimmed = store.stage_speaker is not None and store.stage_speaker != c["id"]
            name = c["name"] if state["actors"][c["id"]].get("known", True) else "???"
            cast.append((c["id"], name, card_image(path) if path else None, (n + 1.0) / (len(present) + 1), dimmed))
        return cast

    def scroll_to_newest(adjustment):
        """Scrolls the text-only story log so the newest turn starts at the top of the view: the
        player's own line first, then the reply, read downwards. Earlier turns are above it."""
        box = renpy.get_widget("turn_input", "story_log_box")
        if box is not None and getattr(box, "offsets", None):
            adjustment.change(min(box.offsets[-1][1], adjustment.range))

    def summarize_if_long(card, state, preset):
        """Folds the older half of the turns the model still sees into the summary. Skipped quietly if the model fails.

        The turns stay in history so the player can still read them.
        """
        limit = preset.get("context", {}).get("summarize_after_turns", 0)
        seen = state["history"][state.get("summarized", 0):]
        if not limit or len(seen) <= limit:
            return
        old = seen[:limit // 2]
        try:
            state["summary"] = run_helper("summarize", aig_prompt.summary_prompt(card, state, old))
        except aig_llm.LLMError:
            return
        state["summarized"] = state.get("summarized", 0) + len(old)

    def start_suggestions():
        """Fetches suggested replies in the background; the input screen fills in when they arrive."""
        store.suggestions = []
        conf = active_preset().get("suggestions", {})
        if not conf.get("enabled", True):
            return
        count, turn = conf.get("count", 3), store.game_state["turn"]
        prompt = aig_prompt.suggest_prompt(current_card(), store.game_state, count)

        def done(reply):
            runtime.suggesting = False
            if not isinstance(reply, Exception) and store.game_state and store.game_state["turn"] == turn:
                store.suggestions = aig_prompt.parse_suggestions(reply, count)
            renpy.restart_interaction()

        runtime.suggesting = True
        run_in_background(lambda: run_helper("suggest_choices", prompt, SUGGEST_SAMPLING, threaded=True), done)

    ## Models screen actions.

    def set_status(table, slot, value):
        table[slot] = value
        renpy.restart_interaction()

    def load_models(slot):
        try:
            request = aig_llm.models_request(slot_connection(slot))
        except aig_llm.LLMError as e:
            return set_status(runtime.models, slot, str(e))
        set_status(runtime.models, slot, "Loading models...")

        def done(result):
            set_status(runtime.models, slot, str(result) if isinstance(result, Exception) else (aig_llm.model_ids(result) or "The provider listed no models."))

        run_in_background(lambda: http_json(request, 30, threaded=True), done)

    def test_model(slot):
        set_status(runtime.tests, slot, "Testing...")

        def done(result):
            set_status(runtime.tests, slot, str(result) if isinstance(result, Exception) else "Working. The model replied.")

        run_in_background(lambda: llm_call(slot, "", [{"role": "user", "content": "Reply with the single word OK."}], {"max_tokens": 200}, threaded=True, what="test"), done)


## Play.

## Always shown under everything else during play: the location's background and whoever is there.
screen stage():
    zorder -10
    $ background = stage_background()

    if background:
        add background fit "cover" xysize (config.screen_width, config.screen_height)
    else:
        add "#14181d"

    for char_id, name, image, x, dimmed in stage_cast():
        if image:
            add image:
                fit "contain"
                ysize 860
                xanchor 0.5
                xpos x
                yalign 1.0
                matrixcolor (BrightnessMatrix(-0.35) if dimmed else None)
        else:
            ## No art for this character: a plain standee with their name.
            frame:
                xysize (340, 620)
                xanchor 0.5
                xpos x
                yalign 1.0
                background (Solid("#22262c") if dimmed else Solid("#3a4048"))
                text esc(name) align (0.5, 0.1) color speaker_color(char_id)


screen thinking():
    frame:
        align (0.5, 0.35)
        padding (60, 40)
        text _("The story continues...")


## A turn as text: what the player said, each paragraph with its speaker, then what changed.
screen turn_text(turn=None):
    vbox:
        spacing 12
        if turn:
            text rich("> " + turn["player"]) color "#aaaaaa"
        for speaker, expression, paragraph in scene_lines(turn):
            if speaker:
                text esc(current_card().characters[speaker]["name"]) color speaker_color(speaker) size 26
            text rich(paragraph)
        for result in (turn["results"] if turn else []):
            text esc(result["message"]) size 24 color ("#9fd89f" if result["ok"] else "#ff8080")


## Where the player answers. The last response stays readable above the input unless the player hides it.
## Cards without assets get the story as a scrolling log on a plain background instead.
screen turn_input():
    default typed = draft
    default story_scroll = ui.adjustment()
    $ card = current_card()

    if not card.visual:
        add "#000000"

    frame:
        xfill True
        padding (60, 20)
        background "#000000b0"
        vbox:
            hbox:
                xfill True
                text esc(card.title) size 30 color "#cccccc" yalign 0.5
                hbox:
                    xalign 1.0
                    spacing 22
                    for name, title in hud_tabs():
                        textbutton title action Show("hud", start=name) text_size 30
                    textbutton _("History") action ShowMenu("history") text_size 30
                    textbutton _("Menu") action ShowMenu() text_size 30
            text esc(status_line()) size 24 color "#cccccc"

    if card.visual and recap_hidden:
        frame:
            align (1.0, 1.0)
            padding (30, 14)
            background "#000000b0"
            textbutton _("Show text") action SetVariable("recap_hidden", False)

    else:
        frame:
            xfill True
            yalign 1.0
            padding (60, 24)
            background "#000000d8"

            vbox:
                spacing 14

                if card.visual:
                    viewport:
                        ysize 300
                        scrollbars "vertical"
                        mousewheel True
                        draggable True
                        use turn_text(game_state["history"][-1] if game_state["history"] else None)
                else:
                    viewport:
                        ysize 560
                        yadjustment story_scroll
                        scrollbars "vertical"
                        mousewheel True
                        draggable True
                        use story_log(TEXT_MODE_TURNS)
                    ## Once the log has been laid out, bring the newest turn to the top of the view.
                    timer 0.05 action Function(scroll_to_newest, story_scroll)

                if turn_error:
                    text esc(turn_error) color "#ff8080"

                if suggestions:
                    vbox:
                        for choice in suggestions:
                            textbutton esc(choice) action Return(choice) text_size 28
                elif runtime.suggesting:
                    text _("Thinking of suggestions...") color "#aaaaaa" size 28

                hbox:
                    spacing 20
                    text _("You:") yalign 0.5
                    frame:
                        xsize 1340
                        padding (16, 10)
                        input value ScreenVariableInputValue("typed", returnable=True) copypaste True multiline True xmaximum 1300
                    textbutton _("Send") action Return(typed) yalign 0.5
                    if card.visual:
                        textbutton _("Hide text") action SetVariable("recap_hidden", True) yalign 0.5

                hbox:
                    spacing 14
                    text (_("Enter starts a new line, Shift+Enter sends.") if persistent.enter_newline else _("Enter sends, Shift+Enter starts a new line.")) size 22 color "#999999" yalign 0.5
                    textbutton _("Switch") action Function(toggle_enter_key) text_size 22 yalign 0.5
                    text _("*italic*  **bold**") size 22 color "#999999" yalign 0.5


## The conversation so far, oldest first. Used by the History menu.
screen story_log(turns=HISTORY_MENU_TURNS):
    $ shown = game_state["history"][-turns:]
    vbox:
        id "story_log_box"
        spacing 30
        if len(shown) == len(game_state["history"]):
            use turn_text(None)
        for turn in shown:
            use turn_text(turn)


## Models.

screen models():
    tag menu

    use game_menu(_("Models"), scroll="viewport"):
        $ settings = llm_settings()
        $ utility = settings["models"]["utility"]

        vbox:
            spacing 30

            use model_slot("main", _("Main model"), _("Writes the story."))

            if usage_lines():
                vbox:
                    spacing 6
                    label _("Token use")
                    for line in usage_lines():
                        text esc(line) size 24
                    text _("The first reply after starting or loading is never cached. A high percentage from the second reply on means prompt caching is working.") size 22 color "#999999"

            vbox:
                style_prefix "check"
                xsize 1100
                textbutton _("Use a separate model for small tasks") action ToggleDict(utility, "enabled")

            if utility["enabled"]:
                use model_slot("utility", _("Utility model"), _("Handles small jobs around the story. Usually a faster, cheaper model."))

                vbox:
                    spacing 6
                    label _("Small tasks")
                    text _("Unticked tasks run on the main model.") size 24
                    vbox:
                        style_prefix "check"
                        xsize 1100
                        for task, title in aig_prompt.HELPER_TASKS:
                            textbutton title action ToggleDict(settings["tasks"], task, "main", "utility") selected (settings["tasks"].get(task, "utility") == "utility")


screen model_slot(slot, title, help):
    $ settings = llm_settings()
    $ entry = settings["models"][slot]
    $ connection = slot_connection(slot)
    $ suggested = aig_llm.SUGGESTED_MODELS.get(connection["provider"], {}).get(slot)

    vbox:
        spacing 10
        label title
        text help size 24

        if slot == "utility":
            vbox:
                style_prefix "check"
                xsize 1100
                textbutton _("Same provider and key as the main model") action ToggleDict(entry, "connection", "main", "utility")

        if entry["connection"] == slot:
            hbox:
                style_prefix "radio"
                spacing 20
                for provider, name in aig_llm.PROVIDERS:
                    textbutton name action SetDict(connection, "provider", provider)

            use settings_input(_("API key"), connection, "api_key", secret=True)
            use settings_input(_("Address"), connection, "base_url", hint=aig_llm.DEFAULT_BASE_URLS[connection["provider"]])

        use settings_input(_("Model"), entry, "model", hint=suggested)

        hbox:
            spacing 30
            if suggested and entry["model"] != suggested:
                textbutton _("Use [suggested]") action SetDict(entry, "model", suggested)
            textbutton _("Choose from list") action [Function(load_models, slot), Show("model_picker", slot=slot)]
            textbutton _("Test") action Function(test_model, slot)

        if slot in runtime.tests:
            text esc(runtime.tests[slot]) size 24


## Searchable, scrollable list of the provider's models. Picking one fills the slot's Model field.
screen model_picker(slot):
    modal True
    zorder 20
    default query = ""
    $ entry = llm_settings()["models"][slot]
    $ listed = runtime.models.get(slot)

    add "#000000c0"

    frame:
        align (0.5, 0.5)
        xsize 1300
        ysize 940
        padding (40, 30)

        vbox:
            spacing 14
            label _("Choose a model")

            hbox:
                spacing 20
                text _("Search") yalign 0.5
                frame:
                    xsize 900
                    padding (16, 10)
                    input value ScreenVariableInputValue("query") copypaste True

            ## Not isinstance(listed, list): inside Ren'Py that name means its own list type.
            if isinstance(listed, str):
                text esc(listed) size 26
            elif listed:
                $ words = query.lower().split()
                $ matches = [m for m in listed if all(w in m.lower() for w in words)]
                text "%d of %d models%s" % (len(matches), len(listed), ". Showing the first 150; search to narrow down." if len(matches) > 150 else "") size 24 color "#aaaaaa"

                viewport:
                    ysize 600
                    scrollbars "vertical"
                    mousewheel True
                    draggable True
                    vbox:
                        for model in matches[:150]:
                            textbutton esc(model):
                                action [SetDict(entry, "model", model), Hide("model_picker")]
                                selected (entry["model"] == model)
                                text_size 26

        textbutton _("Close") action Hide("model_picker") xalign 1.0 yalign 1.0


screen settings_input(title, target, key, secret=False, hint=None):
    $ field = settings_field(target, key)
    hbox:
        spacing 20
        text title yalign 0.5 min_width 180
        button:
            xsize 900
            padding (16, 10)
            background "#00000080"
            action field.Toggle()
            if secret:
                input value field mask "*" copypaste True
            else:
                input value field copypaste True
        if hint and not target[key]:
            text esc(hint) size 24 color "#888888" yalign 0.5
