## LLM settings, calls and the turn loop.

## Device-local model setup, shaped like spec/settings.schema.json. Holds API keys, so it lives in
## persistent data and never in saves.
default persistent.llm = None

## Set when the next turn's text describes something the game has already applied.
default skip_resolve = False
## The player's choice of what Enter does in the story input: False sends, True starts a new line.
default persistent.enter_newline = False
## The player's own copy of the preset's blocks, once they change anything on the Preset screen. None
## means the shipped preset is used as it comes.
default persistent.preset = None
## Which file in the presets folder is in use. None is the one that comes with the game.
default persistent.preset_file = None
## The player's own generation settings, set on the Parameters screen. None until first opened or used,
## then a copy of the preset's values that the player can change. Numbers typed into boxes are kept as text.
default persistent.params = None
## Context sizes learned from providers' model lists, kept so a model's limit is known without asking again.
## "provider|address|model" -> tokens.
default persistent.model_contexts = {}
## Why the last turn failed, or None.
default turn_error = None
## What the player typed for a turn that failed, so they do not have to retype it.
default draft = ""
## Suggested replies for the current turn.
default suggestions = []
## The player folded the reply panel away to see the stage.
default recap_hidden = False
## Character id speaking right now, so the stage can dim everyone else. None between lines.
default stage_speaker = None

init python:
    import copy
    import json
    from aigame import journal as aig_journal
    from aigame import keystore as aig_keystore
    from aigame import llm as aig_llm
    from aigame import wording as aig_wording
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
        # whether the last story reply stopped at the response length limit
        cut_short = False
        # True while a turn is being worked out. Saving is switched off for that long.
        busy = False
        # the model whose context size is being looked up right now, so it is only asked for once
        context_lookup = None
        # how the instruction being edited on the Preset screen stood before the editor opened
        preset_edit = None
        # the same for one of the game's own prompts: {"key", "had", "before"}
        wording_edit = None
        # The connection pool shared by every request. See http_session.
        http = None
        # Shown while a reply is awaited, when something worth knowing is going on (a connection being retried).
        wait_note = ""
        # How many words of the story reply have arrived so far, and whether the last one broke off because the connection went.
        wait_words = 0
        reply_dropped = False
        # What a turn that failed part-way had already got from the models. See run_turn.
        kept_turn = None
        # the input area's layout the text-only story was last fitted to. See story_layout.
        story_layout = None
        # what the last preset import did, shown on the Preset screen
        preset_message = ""
        # the same for the journal's "write one now", and the entry being edited: {"id", "before"}
        journal_message = ""
        journal_edit = None
        # API keys, by connection id, while the game runs. They are never part of the saved settings:
        # see the API keys section below. keys_stored is what the system's store holds, to know what changed.
        keys = {}
        keys_stored = {}

    runtime = Runtime()

    ## Settings.

    ## The jobs that keep the story's memory. Each can be given to the main model, to the model for
    ## small tasks, or to a model of its own (the "memory" slot), since remembering well is worth a
    ## better model than tidying up is, and worth a cheaper one than writing the story.
    MEMORY_TASKS = (("write_journal", _("The journal is written by")), ("summarize", _("The summary is written by")))

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
        settings = persistent.llm
        ## Settings saved before the memory model existed gain its slot, empty and unused.
        if "memory" not in settings["models"]:
            settings["models"]["memory"] = {"connection": "main", "model": ""}
        if not any(c["id"] == "memory" for c in settings["connections"]):
            settings["connections"].append({"id": "memory", "provider": settings["connections"][0]["provider"], "base_url": "", "api_key": ""})
        return settings

    def llm_connection(connection_id):
        for connection in llm_settings()["connections"]:
            if connection["id"] == connection_id:
                return connection

    def slot_connection(slot):
        return llm_connection(llm_settings()["models"][slot]["connection"])

    ## API keys. A key is kept by the operating system (aigame/keystore.py), not in the settings
    ## Ren'Py saves, because those sit in the game's folder as a file anyone can read. The saved
    ## settings keep an empty "api_key" for each connection; the real one lives in runtime.keys.

    def key_name(connection_id):
        return "connection:" + connection_id

    def adopt_keys():
        """At start: fetches each connection's key from the system's store. A key found in the saved
        settings, from before keys were kept apart, is moved to the store and wiped from the settings."""
        if renpy.android or renpy.ios or renpy.emscripten:
            aig_keystore.setup(config.savedir)
        moved = False
        for connection in llm_settings()["connections"]:
            name, old = key_name(connection["id"]), (connection.get("api_key") or "").strip()
            kept = bool(old) and aig_keystore.put(name, old)
            if kept:
                connection["api_key"] = ""
                moved = True
            runtime.keys[connection["id"]] = old or aig_keystore.get(name)
            ## If the store refused, the key stays in the settings for now and is offered to the store again later.
            runtime.keys_stored[connection["id"]] = "" if old and not kept else runtime.keys[connection["id"]]
        if moved:
            renpy.save_persistent()

    config.start_callbacks.append(adopt_keys)

    def save_keys():
        """Hands any key the player has typed or changed to the system's store."""
        for connection_id, value in list(runtime.keys.items()):
            value = value.strip()
            if value != runtime.keys_stored.get(connection_id, "") and aig_keystore.put(key_name(connection_id), value):
                runtime.keys_stored[connection_id] = value

    def request_connection(slot):
        """The slot's connection with its key filled in, for making a request. Never stored anywhere."""
        connection = slot_connection(slot)
        save_keys()
        return dict(connection, api_key=runtime.keys.get(connection["id"], "") or connection.get("api_key", ""))

    def keep_settings():
        """Writes the settings to disk now. Ren'Py would get to it eventually, but a model that was
        just chosen must not be lost to a crash, a reload or the window being closed."""
        save_keys()
        renpy.save_persistent()

    def llm_ready():
        return bool(llm_settings()["models"]["main"]["model"].strip())

    def task_choice(task):
        """Which model the player has given a helper job to: "main", "utility" or "memory". A job left
        alone goes to the model for small tasks; a choice that cannot be honoured falls back in the
        order memory, utility, main, so a job never stops running because a slot was emptied."""
        settings = llm_settings()
        wanted = settings["tasks"].get(task, "utility")
        utility = settings["models"]["utility"]
        if wanted == "memory" and settings["models"]["memory"]["model"].strip():
            return "memory"
        if wanted in ("utility", "memory") and utility.get("enabled") and utility["model"].strip():
            return "utility"
        return "main"

    def slot_for(task):
        """The model slot a helper task runs on."""
        return task_choice(task)

    input_fields = {}

    def settings_field(target, key):
        """One editable text field of the settings. Cached so a field keeps focus while the screen redraws."""
        if (id(target), key) not in input_fields:
            input_fields[id(target), key] = DictInputValue(target, key, default=False)
        return input_fields[id(target), key]

    ## Calls.

    ## Sending a request again by itself is only right when the first one cannot have been answered.
    ## Otherwise the model writes the same reply twice: twice the wait, twice the cost, for nothing.
    ## So a request is sent again only if the connection was never made at all, or if it failed so
    ## soon that no reply can have been written yet. A connection that drops after a long wait is
    ## never retried here: the reply may well exist, and it is the player's call to send again.
    CONNECT_RETRIES = (1.0, 2.0, 4.0)   # how long to wait before each further go
    ## How long a connection may take to be set up. A healthy one takes well under a second. One that
    ## is going to fail tends to hang for a quarter of a minute first and then break in a way that
    ## cannot be told from a reply lost on its way back, which must not be retried. Giving up on the
    ## setting-up early turns that into a plain "never connected", which is safe to try again.
    CONNECT_TIMEOUT = 6

    def http_session():
        """One connection pool for the whole game, so that the calls of a turn reuse a connection
        that is already open where they can: every new connection is a new chance for it to fail."""
        import requests
        if runtime.http is None:
            runtime.http = requests.Session()
        return runtime.http
    TOO_SOON_FOR_A_REPLY = 5.0          # seconds

    def never_connected(error):
        """Whether a failed request failed while the connection was still being set up, before a
        single byte of it can have reached the provider."""
        import requests
        original = getattr(error, "original_exception", error)
        if isinstance(original, requests.exceptions.ReadTimeout):
            return False
        if isinstance(original, (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError, requests.exceptions.ProxyError)):
            return True
        text = str(original)
        return isinstance(original, requests.exceptions.ConnectionError) and any(
            sign in text for sign in ("Failed to establish a new connection", "Connection refused", "NameResolution", "nodename nor servname", "Name or service not known"))

    def dropped(error):
        """Whether a failed request lost its connection at some point that cannot be told: perhaps
        before it was sent, perhaps while the reply was on its way back."""
        import requests
        original = getattr(error, "original_exception", error)
        return isinstance(original, requests.exceptions.ConnectionError) and not isinstance(original, requests.exceptions.ReadTimeout)

    def sent_with_care(send, threaded):
        """Calls send() and returns what it returns. See the note above for when a failed request is
        sent again without asking."""
        import time
        for go, wait in enumerate(CONNECT_RETRIES + (None,)):
            started = time.time()
            try:
                result = send()
                runtime.wait_note = ""
                return result
            except aig_llm.LLMError as e:
                safe = getattr(e, "never_connected", False) or (getattr(e, "dropped", False) and time.time() - started < TOO_SOON_FOR_A_REPLY)
                if wait is None or not safe:
                    runtime.wait_note = ""
                    if getattr(e, "dropped", False) and not safe:
                        raise aig_llm.LLMError("The connection was lost after %d seconds of waiting for the reply. The provider may already have written it, "
                                               "so the game has not sent the request again by itself. Send again when you are ready. (%s)" % (time.time() - started, e))
                    raise
            runtime.wait_note = "The connection to the provider failed before anything was sent. Trying again (%d of %d)..." % (go + 2, len(CONNECT_RETRIES) + 1)
            if threaded:
                time.sleep(wait)
            else:
                renpy.pause(wait, hard=True)

    def http_json(request, timeout, threaded=False):
        """Sends a request built by aig_llm and returns the decoded JSON body."""
        return sent_with_care(lambda: http_json_once(request, timeout, threaded), threaded)

    def unreachable(e):
        error = aig_llm.LLMError(aig_llm.describe_http_error(None, None) + " " + str(e))
        error.never_connected, error.dropped = never_connected(e), dropped(e)
        return error

    ## The story reply is asked for as a stream (see aigame/llm.py): it arrives as it is written, so
    ## the connection is never left silent for the networks in between to drop, the player can see
    ## that something is coming, and if the connection does go, what had arrived is not lost.
    STREAM_SILENCE = 120        # seconds without a single piece before the reply is given up on
    WORTH_KEEPING = 200         # characters; less than this of a broken-off reply is no reply

    def http_stream_once(request, provider):
        """Sends a streamed chat request and returns the reply in the shape of a whole one. Runs on a
        thread. A reply that stops arriving part-way is returned as far as it got, marked "_dropped"."""
        import requests
        reader = aig_llm.StreamReader(provider)
        runtime.wait_words = 0
        try:
            response = http_session().post(request["url"], json=request["json"], headers=request["headers"], timeout=(CONNECT_TIMEOUT, STREAM_SILENCE), stream=True)
            if response.status_code >= 400:
                raise aig_llm.LLMError(aig_llm.describe_http_error(response.status_code, response.text))
            if "event-stream" not in (response.headers.get("Content-Type") or ""):
                return response.json()          # an endpoint that ignores the request to stream and answers whole
            for line in response.iter_lines():
                if line:
                    reader.feed(line)
                    runtime.wait_words = reader.text.count(" ")
        except aig_llm.LLMError:
            raise
        except Exception as e:
            if len(reader.text.strip()) >= WORTH_KEEPING:
                return dict(reader.data(cut_off=True), _dropped=True)
            raise unreachable(e)
        if reader.finish is None and not reader.error and len(reader.text.strip()) >= WORTH_KEEPING and not reader.done:
            return dict(reader.data(cut_off=True), _dropped=True)       # the stream simply ended, with no word that the reply was complete
        return reader.data()

    def http_json_once(request, timeout, threaded=False):
        try:
            if threaded and not renpy.emscripten:
                # renpy.fetch pumps the display while it waits, which is only safe on the main thread.
                import requests
                response = http_session().request("POST" if "json" in request else "GET", request["url"],
                                                  json=request.get("json"), headers=request["headers"], timeout=(CONNECT_TIMEOUT, timeout))
                if response.status_code >= 400:
                    raise aig_llm.LLMError(aig_llm.describe_http_error(response.status_code, response.text))
                return response.json()
            return renpy.fetch(request["url"], json=request.get("json"), headers=request["headers"], timeout=timeout, result="json")
        except renpy.FetchError as e:
            response = getattr(e.original_exception, "response", None)
            if response is not None:
                raise aig_llm.LLMError(aig_llm.describe_http_error(response.status_code, response.text))
            raise unreachable(e)
        except aig_llm.LLMError:
            raise
        except Exception as e:
            raise unreachable(e)

    def llm_call(slot, system, messages, sampling, threaded=False, what="story"):
        connection = request_connection(slot)
        if store.game_state is not None and store.card_name:
            ## Every prompt, whichever job built it, has the card's placeholders filled in here, so a
            ## raw {{user}} can never reach a model. Filling twice changes nothing.
            card, state = current_card(), store.game_state
            system = aig_prompt.fill(card, state, system)
            messages = [dict(m, content=aig_prompt.fill(card, state, m["content"])) for m in messages]
        model = llm_settings()["models"][slot]["model"].strip()
        request = aig_llm.chat_request(connection, model, system, messages, sampling)
        whole = lambda: http_json(request, CHAT_TIMEOUT, False)
        if threaded:
            data = http_json(request, CHAT_TIMEOUT, True)
        elif what == "story" and params().get("stream", True):
            streamed = aig_llm.stream_request(connection, model, system, messages, sampling)
            data = away_from_the_screen(lambda: sent_with_care(lambda: http_stream_once(streamed, connection["provider"]), True), whole)
        else:
            data = away_from_the_screen(lambda: http_json(request, CHAT_TIMEOUT, True), whole)
        runtime.wait_words = 0
        runtime.usage = runtime.usage[-39:] + [dict(aig_llm.chat_usage(connection["provider"], data), what=what)]
        if what == "story":
            runtime.cut_short = aig_llm.chat_cut_short(connection["provider"], data)
            runtime.reply_dropped = bool(isinstance(data, dict) and data.get("_dropped"))
        return aig_llm.chat_text(connection["provider"], data)

    ## Waiting for a model. The request runs on another thread while this one looks in a few times a
    ## second and otherwise sleeps. Ren'Py's own fetch keeps the whole screen redrawing as fast as it
    ## can for as long as it waits, which is most of the time a turn takes: it had one processor core
    ## flat out, and a laptop's fan running, for nothing.
    WAIT_TICK = 0.25

    def wait_for_threads(threads):
        while any(thread.is_alive() for thread in threads):
            renpy.pause(WAIT_TICK, hard=True)

    def away_from_the_screen(work, on_this_thread):
        """Runs work() on another thread and waits for it quietly; returns what it returned or raises
        what it raised. Where that is not possible (the web build has no threads; a button's action
        cannot wait this way), on_this_thread() is called instead."""
        import threading
        if renpy.emscripten or renpy.game.context().interacting:
            return on_this_thread()
        outcome = {}

        def body():
            try:
                outcome["value"] = work()
            except BaseException as e:
                outcome["error"] = e

        thread = threading.Thread(target=body)
        thread.daemon = True
        thread.start()
        wait_for_threads([thread])
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    ## A helper's answer is only worth having if the game can read it. An answer that is not the JSON
    ## it was asked for used to be taken as "nothing to report", so a turn could go through with its
    ## changes silently unrecorded. Now such an answer counts as a failure: the helper is asked once
    ## more, and if that fails too, the turn does not go through at all.
    HELPER_TRIES = 2

    def run_helper_sure(task, prompt, key, threaded=False):
        """run_helper, insisting on a JSON answer with a list under key. Raises LLMError otherwise.
        Only an answer that came back unreadable is asked for again. A call that failed outright is
        not repeated here: whether that is safe is decided in http_json, which knows how it failed."""
        for attempt in range(HELPER_TRIES):
            reply = run_helper(task, prompt, threaded=threaded)
            if aig_prompt.answers_with(reply, key):
                return reply
        raise aig_llm.LLMError("Its answer was not in the form the game reads.")

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

    def run_helpers_together(jobs):
        """Runs several helper jobs at once and waits for them all. jobs maps a name to
        (task, prompt, key), key being the list its JSON answer must have. The result maps each name
        to the reply text, or to the error that job ended with.

        They only read the story, so nothing they do depends on another's answer, and running them
        side by side costs the player the wait of the slowest one, not of all of them added up.
        """
        answers = {}

        def work(name, task, prompt, key, threaded):
            try:
                answers[name] = run_helper_sure(task, prompt, key, threaded=threaded)
            except Exception as e:
                answers[name] = e

        if renpy.emscripten or len(jobs) < 2:
            for name, (task, prompt, key) in jobs.items():
                work(name, task, prompt, key, False)
            return answers

        import threading
        threads = [threading.Thread(target=work, args=(name, task, prompt, key, True)) for name, (task, prompt, key) in jobs.items()]
        for thread in threads:
            thread.daemon = True
            thread.start()
        wait_for_threads(threads)
        return answers

    def usage_brief():
        """One short line about the last story reply's cost, for the play screen. Empty before the first reply."""
        story = [c for c in runtime.usage if c["what"] == "story"]
        if not story:
            return ""
        sent, cached = story[-1]["input"], story[-1]["cached"]
        if not sent:
            return "Tokens: not reported by this provider"
        return "Last reply: %s tokens sent, %d%% from cache" % ("{:,}".format(sent), round(100.0 * cached / sent))

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

    def preset_folder():
        return os.path.join(data_dir(), "presets")

    def read_preset(name):
        """One file from the presets folder, read again whenever it changes on disk, so a preset
        being worked on in the creator is picked up without restarting the game. None if the file
        is missing or the game cannot use it."""
        path = os.path.join(preset_folder(), name + ".preset.json")
        try:
            stamp = os.path.getmtime(path)
            if preset_cache.get(name, (None, None))[0] != stamp:
                with open(path, "rb") as f:
                    loaded = json.loads(f.read().decode("utf-8"))
                preset_cache[name] = (stamp, None if aig_wording.check_preset(loaded) else loaded)
            return preset_cache[name][1]
        except (IOError, OSError, ValueError):
            return None

    def preset_files():
        """(file name without its ending, the preset's own name) for every usable preset, the game's own first."""
        try:
            names = sorted(n[:-len(".preset.json")] for n in os.listdir(preset_folder()) if n.endswith(".preset.json"))
        except OSError:
            names = []
        names = [n for n in names if read_preset(n)]
        return [(n, read_preset(n)["name"]) for n in sorted(names, key=lambda n: (n != "default", n))]

    def shipped_preset():
        """The preset file in use: the one the player chose, or the one that comes with the game."""
        return (persistent.preset_file and read_preset(persistent.preset_file)) or read_preset("default")

    def active_preset():
        """The preset in use: the chosen file, with whatever the player changed on the Preset screen
        laid over it. Their instructions replace the file's as a whole once they touch any; each of
        the game's own prompts they reword replaces only that prompt."""
        shipped, mine = shipped_preset(), persistent.preset or {}
        prompts = dict(shipped.get("prompts") or {})
        prompts.update(mine.get("prompts") or {})
        return dict(shipped, blocks=mine.get("blocks") or shipped["blocks"], prompts=prompts)

    def own_blocks():
        return bool(persistent.preset and persistent.preset.get("blocks"))

    def preset_changed():
        return bool(persistent.preset and (persistent.preset.get("blocks") or persistent.preset.get("prompts")))

    def preset_blocks():
        """The player's own copy of the blocks, made the first time they change something."""
        if not own_blocks():
            blocks = copy.deepcopy(shipped_preset()["blocks"])
            for block in blocks:
                block.setdefault("enabled", True)
            if persistent.preset is None:
                persistent.preset = {}
            persistent.preset["blocks"] = blocks
        return persistent.preset["blocks"]

    def drop_own(key):
        """Forgets the player's own blocks or prompts, and the whole record once nothing is left in it."""
        if persistent.preset:
            persistent.preset.pop(key, None)
            if not persistent.preset.get("blocks") and not persistent.preset.get("prompts"):
                persistent.preset = None

    def preset_import():
        """Asks for a preset file someone shared, adds it to the presets folder and switches to it."""
        runtime.preset_message = ""
        try:
            path = pick_file("Choose a preset file", "SI-Station presets (*.json)|*.json|")
        except Exception:
            runtime.preset_message = "No file chooser is available here. Copy the preset file into the game's presets folder instead."
            path = None
        if path:
            try:
                name = aig_wording.import_preset(path, preset_folder())
                preset_choose(name)
                runtime.preset_message = "Imported \"%s\" and switched to it." % read_preset(name)["name"]
            except aig_wording.PresetError as e:
                runtime.preset_message = str(e)
        renpy.restart_interaction()

    def preset_choose(name):
        """Switches to another preset file. The player's own changes belonged to the old one."""
        persistent.preset_file = None if name == "default" else name
        persistent.preset = None
        input_fields.clear()
        renpy.restart_interaction()

    def reset_preset():
        persistent.preset = None
        input_fields.clear()
        renpy.restart_interaction()

    def preset_toggle(index):
        block = preset_blocks()[index]
        block["enabled"] = not block.get("enabled", True)
        renpy.restart_interaction()

    def preset_move(index, step):
        blocks = preset_blocks()
        if 0 <= index + step < len(blocks):
            blocks[index], blocks[index + step] = blocks[index + step], blocks[index]
        renpy.restart_interaction()

    def preset_delete(index):
        del preset_blocks()[index]
        renpy.restart_interaction()

    def preset_add(after_history):
        """Adds an empty instruction and returns its position, so the editor can open on it."""
        blocks = preset_blocks()
        taken = set(b["id"] for b in blocks)
        n = 1
        while "custom_%d" % n in taken:
            n += 1
        block = {"id": "custom_%d" % n, "name": "My instruction", "kind": "text", "content": "", "enabled": True}
        history = next((i for i, b in enumerate(blocks) if b.get("slot") == "history"), len(blocks) - 1)
        index = len(blocks) if after_history else history
        blocks.insert(index, block)
        return index

    def preset_begin_edit(index, new, had_copy):
        """Remembers how things stood before the editor opened, so Cancel can put them back."""
        runtime.preset_edit = {"index": index, "new": new, "had_copy": had_copy,
                               "before": None if new else copy.deepcopy(preset_blocks()[index])}
        renpy.show_screen("preset_edit", index=index)
        renpy.restart_interaction()

    def preset_open_editor(index):
        """Opens an instruction for editing. Returns nothing: a value returned by a button's function
        closes the menu the button is on."""
        had_copy = own_blocks()
        preset_blocks()         # editing works on the player's own copy, so make it if this is the first change
        preset_begin_edit(index, False, had_copy)

    def preset_add_and_edit(after_history):
        had_copy = own_blocks()
        preset_begin_edit(preset_add(after_history), True, had_copy)

    def preset_cancel_edit():
        """Leaves the editor as if it had never been opened: an edited instruction gets its old name
        and wording back, and a newly added one is removed."""
        edit, blocks = runtime.preset_edit, preset_blocks()
        ## Close the editor before touching the list: it is drawn once more as it closes, and must
        ## not go looking for an instruction that has just been removed.
        renpy.hide_screen("preset_edit")
        if edit["new"]:
            del blocks[edit["index"]]
        else:
            blocks[edit["index"]].clear()
            blocks[edit["index"]].update(edit["before"])
        if not edit["had_copy"]:
            drop_own("blocks")       # nothing else had been changed, so go back to the preset file's own
        input_fields.clear()
        renpy.restart_interaction()

    ## The game's own prompts (aigame/wording.py). Each can be reworded; the player's wording is kept
    ## in persistent.preset["prompts"] and only for the prompts they actually changed.

    def wording_default(key):
        """What the prompt says when the player has not reworded it: the preset file's wording, else the game's."""
        return (shipped_preset().get("prompts") or {}).get(key) or aig_wording.builtin_prompt(key)["text"]

    def wording_is_own(key):
        return bool(persistent.preset and key in (persistent.preset.get("prompts") or {}))

    def wording_open_editor(key):
        if persistent.preset is None:
            persistent.preset = {}
        mine = persistent.preset.setdefault("prompts", {})
        runtime.wording_edit = {"key": key, "had": key in mine, "before": mine.get(key)}
        mine.setdefault(key, wording_default(key))
        renpy.show_screen("wording_edit", key=key)
        renpy.restart_interaction()

    def wording_close(keep):
        """Done keeps the new wording, unless it is the same as the default after all. Cancel puts back what was there."""
        edit, mine = runtime.wording_edit, persistent.preset["prompts"]
        renpy.hide_screen("wording_edit")
        if not keep and edit["had"]:
            mine[edit["key"]] = edit["before"]
        elif not keep or not mine[edit["key"]].strip() or mine[edit["key"]] == wording_default(edit["key"]):
            del mine[edit["key"]]
        if not mine:
            drop_own("prompts")
        input_fields.clear()
        renpy.restart_interaction()

    def wording_reset(key):
        persistent.preset["prompts"].pop(key, None)
        if not persistent.preset["prompts"]:
            drop_own("prompts")
        input_fields.clear()
        renpy.restart_interaction()

    def preset_note(block):
        """What a built-in part of the prompt is, in the player's terms."""
        return {
            "world": _("The card's world and opening situation."),
            "narrator_instructions": _("The card creator's own guidance to the narrator."),
            "persona": _("Who you are playing."),
            "characters": _("The card's cast."),
            "action_protocol": _("How the game's rules work. Always sent."),
            "history": _("The story so far. Everything above this line is sent once and then cached; everything below is sent fresh each turn."),
            "summary": _("The summary of older turns, once there is one."),
            "lorebook": _("Background facts and memories that have just become relevant."),
            "state": _("Health, items, places, who is present: the game's current state."),
            "quests": _("Open quests and their objectives."),
        }.get(block.get("slot"), block.get("help", ""))

    ## The turn.

    ## Loading a save does not always resume where the player was looking. Ren'Py resumes at the last
    ## statement that paused, and a turn pauses many times while it waits for the model, so a save made
    ## at the input screen could resume inside the turn before it and play that turn a second time.
    ## Two things prevent that. open_turn makes the input screen itself the place to resume. And the
    ## notes of what is in progress are kept inside game_state, not in store variables: Ren'Py winds
    ## store variables back to the start of the statement on load, but leaves game_state as saved.

    def open_turn():
        """Marks that the game is waiting for the player, and makes this the point a save resumes from."""
        store.game_state["open"] = True
        renpy.checkpoint()

    def restore_turn(state):
        """Puts the state back to how it was before the turn in progress began."""
        before = state["restore_point"]
        state.clear()
        state.update(before)

    def set_busy(on):
        """Switches saving off while the story is being written, and back on when the reply is in.
        A save made halfway through a turn has nothing sensible to resume to."""
        runtime.busy = on
        store._autosave = not on

    ## Undo. Before each turn the whole game state is copied. Taking a turn back puts that copy in
    ## place, so everything the turn changed goes back with it: what the story model wrote, what the
    ## bookkeeper recorded, where the scene director put people, quest progress, places the story
    ## made, the summary. Nothing is patched up piece by piece, so nothing can be missed. The last
    ## UNDO_DEPTH turns are kept. A copy leaves out the story so far, which only ever grows at its
    ## end and is cut back to its old length instead.

    UNDO_DEPTH = 10

    def undo_copies(state):
        """The copies undo can return to, oldest first. A save from before several were kept has one, held whole."""
        copies = list(state.get("undo_stack") or [])
        old = state.get("undo_point")
        if old is not None and not copies:
            old = dict(old)
            old["history_len"] = len(old.pop("history", []))
            copies = [old]
        return copies

    def can_undo():
        return bool(store.game_state and undo_copies(store.game_state))

    def undo_turn():
        """Takes back the last turn: the story, and everything it changed, go back to exactly how they
        were before it, and what the player typed is put back in the input box to edit or resend.
        Pressing it again takes back the turn before, for as many turns as copies are kept."""
        state = store.game_state
        copies = undo_copies(state)
        if not state["history"] or not copies:
            return
        typed = state["history"][-1]["player"]
        before = copies.pop()
        story = state["history"][:before.pop("history_len")]
        state.clear()
        state.update(before)
        state["history"] = story
        state["undo_stack"] = copies
        state["open"] = True
        store.suggestions = []
        store.turn_error = None
        screen = renpy.get_screen("turn_input")
        if screen is not None:
            screen.scope["typed"] = "" if typed.startswith("(") else typed     # "(The fight is over.)" and the like are not the player's words
        renpy.restart_interaction()

    def play_turn(text, resolve=True):
        """One full turn, with saving switched off until it is over. See run_turn."""
        set_busy(True)
        try:
            run_turn(text, resolve)
        finally:
            set_busy(False)

    def run_turn(text, resolve):
        """Works out what the player attempts, applies it, narrates, and applies what the narration changed.

        resolve is False when the game already knows what the player did (they pressed a button, or
        a fight just ended), so there is nothing to work out from the text.

        On failure the state is put back as it was and turn_error says why.
        """
        store.turn_error = None
        state = store.game_state
        if state.get("restore_point") is not None:
            # Reached again after loading a save made while this turn was waiting on the model: start it over.
            restore_turn(state)
        if not state.get("open", True):
            # Reached again after loading a save made once this turn was already played. Nothing to do.
            return
        ## The copy must not carry older copies inside it, or saves would grow with every turn.
        state["restore_point"] = None
        earlier_undo = undo_copies(state)
        state.pop("undo_point", None)
        state.pop("undo_stack", None)
        state["restore_point"] = copy.deepcopy(state)
        card, preset = current_card(), active_preset()
        keeper = bookkeeping()

        ## A turn is several calls to models, and it either goes through whole or not at all: if any
        ## part of it cannot be had, everything is put back and the player is told. What the models
        ## had already answered is kept, though, so that sending the same thing again picks up where
        ## it failed, and nothing is written, or paid for, a second time.
        key = (state["turn"], len(state["history"]), text, resolve, keeper)
        kept = runtime.kept_turn if runtime.kept_turn and runtime.kept_turn["key"] == key else {"key": key, "have": {}}
        runtime.kept_turn = kept
        have = kept["have"]

        def once(name, ask):
            if name not in have:
                have[name] = ask()
            return have[name]

        stage = "work out what you are doing"
        try:
            ## A card with nothing the player can act on skips the call that works out their actions.
            attempts = []
            if resolve and aig_prompt.player_action_types(card):
                attempts = aig_prompt.parse_resolver(once("attempts", lambda: run_helper_sure(
                    "resolve_actions", aig_prompt.resolver_prompt(card, state, text, prompts=preset["prompts"]), "actions")), card)
            results = state["pending_results"] + aig_actions.apply_actions(card, state, attempts, by_player=True)
            ## Gagged, blinded or deafened: the story model is told how the player's message lands.
            results = results + aig_prompt.sense_notes(card, state)

            ## Whoever the player just named, or the last reply did, enters the story with this turn. The
            ## story model is told who they are in this turn's message, which is kept as it was sent.
            entering = aig_prompt.arrivals(card, state, text)
            stage = "write the story"
            system, messages = aig_prompt.narrator_prompt(card, state, preset, text, results, record=not keeper, check=params().get("check_first", False), header=params().get("header", True))
            reply, cut_short, broke_off = once("reply", lambda: (llm_call("main", system, messages, story_sampling()), runtime.cut_short, runtime.reply_dropped))
            runtime.cut_short = cut_short
            narration, world_actions = aig_prompt.parse_narration(reply)
            ## The time and place line that heads the reply is not part of the story: it is shown in the bar at the top.
            clock, narration = aig_prompt.split_header(narration) if params().get("header", True) else (None, narration)
            narration = aig_prompt.fill(card, state, narration)     # in case the model wrote the placeholder back
            if not narration:
                del have["reply"]
                raise aig_llm.LLMError("The model returned no story text.")
            if keeper:
                ## The story model only wrote prose. Two narrowly focused helpers read it at the same
                ## time: the bookkeeper records what changed, and the quest judge alone decides whether
                ## a quest has moved on. Both must answer, or the turn does not go through.
                stage = "record what the reply changed"
                judged = bool(card.quests)
                jobs = {"books": ("record_changes", aig_prompt.bookkeeper_prompt(card, state, text, results, narration, quests=not judged, prompts=preset["prompts"], clock=(state.get("header"), clock)), "actions")}
                if judged:
                    jobs["quests"] = ("judge_quests", aig_prompt.judge_prompt(card, state, text, narration, prompts=preset["prompts"]), "verdicts")
                failed = {}
                for name, answer in run_helpers_together(dict((name, job) for name, job in jobs.items() if name not in have)).items():
                    if not isinstance(answer, Exception):
                        have[name] = answer
                    elif isinstance(answer, aig_llm.LLMError):
                        failed[name] = answer
                    else:
                        raise answer
                for name, who in (("books", "The bookkeeper"), ("quests", "The quest judge")):
                    if name in failed:
                        raise aig_llm.LLMError("%s: %s" % (who, failed[name]))
                world_actions = aig_prompt.parse_bookkeeper(have["books"])
                if judged:
                    ## Quests are the judge's alone. Anything else that tries to move one is ignored.
                    world_actions = [a for a in world_actions if not str(a.get("type", "")).startswith("quest_")]
                    ## The judge's verdicts go first: a quest finished by this text pays its reward before
                    ## the bookkeeper's report of the same reward being handed over is looked at.
                    world_actions = aig_prompt.parse_judge(have["quests"], card) + world_actions
            results = results + aig_actions.apply_actions(card, state, world_actions)

            state["pending_results"] = []
            stage = "direct the scene"
            direction = direct_scene(card, state, narration, once=once)
        except aig_llm.LLMError as e:
            restore_turn(state)
            state["open"] = False
            state["undo_stack"] = earlier_undo
            if "reply" in have:
                store.turn_error = ("The reply was written, but the game could not %s, so nothing has been changed. (%s) "
                                    "Send the same message again: what was already done is kept and will not be done, or paid for, twice.") % (stage, e)
            else:
                store.turn_error = "The game could not %s, so nothing has been changed. (%s)" % (stage, e)
            store.draft = text
            return

        runtime.kept_turn = None
        turn = {"player": text, "results": [{"ok": r["ok"], "message": r["message"]} for r in results], "cast": entering,
                "narration": narration, "direction": direction}
        ## Where the player was and who with when this turn began, for the journal to tell arrivals and reunions by.
        turn.update(aig_journal.scene_facts(state["restore_point"]))
        ## Who was there for this turn: with the player as it began or as it ended, or speaking in it.
        ## The story model is shown this with every past turn, to tell what each character can know.
        if clock:
            turn["header"] = state["header"] = aig_prompt.fill(card, state, clock)
        turn["there"] = sorted(set(turn["with"]) | set(aig_journal.scene_facts(state)["with"]) | set(d["speaker"] for d in direction if d.get("speaker")))
        if broke_off:
            turn["notice"] = ("The connection to the provider was lost while this reply was arriving, so it stops early. What had arrived is kept, "
                              "and the game has recorded what it shows. Carry on from here, or undo the turn and send it again.")
        elif runtime.cut_short:
            ## Kept apart from the results: it is for the player, and is never sent to the model.
            turn["notice"] = ("This reply was cut off at the response length limit (%d tokens), so it may end mid-sentence and "
                              "anything it changed at the very end may be missing. Raise Response length in Menu > Settings > Parameters.") % story_sampling()["max_tokens"]
        ## From here the turn counts as played: nothing below may pause before these three lines are done.
        state["history"].append(turn)
        state["cast"] = state.get("cast", []) + [c for c in entering if c not in state.get("cast", [])]
        state["turn"] += 1
        state["open"] = False
        ## What the turn started from becomes what Undo goes back to.
        before, state["restore_point"] = state["restore_point"], None
        before["history_len"] = len(before.pop("history"))
        state["undo_stack"] = (earlier_undo + [before])[-UNDO_DEPTH:]
        store.game_log = (store.game_log + results)[-30:]
        store.draft = ""
        keep_journal(card, state, before, text, preset)
        move_world(card, state, before, preset)
        summarize_if_long(card, state, preset)

    ## The journal (aigame/journal.py): a short entry about each scene, written when the scene ends
    ## and sent back to the story model on later turns where it matters.

    def write_journal_entry(card, state, start, end, place, preset):
        """Asks the helper for an entry about history[start:end] and files it. Returns whether it worked."""
        try:
            reply = run_helper("write_journal", aig_prompt.journal_prompt(card, state, state["history"][start:end], prompts=preset["prompts"]))
        except aig_llm.LLMError:
            return False
        written = aig_journal.parse_entry(reply)
        if written is None:
            return False
        aig_journal.add(card, state, start, end, place, written)
        return True

    WORLD_EVERY = 15        # turns between two looks at where everyone has got to, when the player stays put

    def move_world(card, state, before, preset):
        """After a turn: when the player has gone somewhere else, or has stayed put for a good while,
        a helper works out where the people they know have got to meanwhile. Without it everyone
        stays where the story last showed them. A failed attempt changes nothing and is tried again
        at the next occasion."""
        if not params().get("world", True) or not aig_prompt.offstage(card, state):
            return
        went = before["actors"]["player"]["location"] != state["actors"]["player"]["location"]
        if not went and state["turn"] - state.get("world_at", 0) < WORLD_EVERY:
            return
        try:
            reply = run_helper_sure("move_world", aig_prompt.world_prompt(card, state, prompts=preset["prompts"]), "whereabouts")
        except aig_llm.LLMError:
            return
        aig_state.track(card, state, aig_prompt.parse_world(reply, card, state))
        state["world_at"] = state["turn"]

    def keep_journal(card, state, before, text, preset):
        """After a turn: if a scene has just ended, write its entry. A failed attempt costs nothing but
        the entry; the same turns are offered again at the next scene end."""
        if not params().get("journal", True):
            return
        scene = aig_journal.due(before, state, text)
        if scene is not None:
            write_journal_entry(card, state, scene[0], scene[1], scene[2], preset)

    def journal_waiting():
        """How many turns have been played since the last journal entry."""
        state = store.game_state
        return len(state["history"]) - min(state.get("journal_upto", 0), len(state["history"]))

    def journal_now():
        """The player asks for an entry about everything since the last one, wherever the scene stands."""
        state = store.game_state
        start, end = min(state.get("journal_upto", 0), len(state["history"])), len(state["history"])
        if end <= start:
            return
        set_busy(True)
        try:
            done = write_journal_entry(current_card(), state, max(start, end - aig_journal.LONGEST), end, state["actors"]["player"]["location"], active_preset())
        finally:
            set_busy(False)
        runtime.journal_message = "" if done else "The entry could not be written just now. Try again in a moment."
        renpy.restart_interaction()

    def journal_entry(entry_id):
        for entry in store.game_state.get("journal", []):
            if entry["id"] == entry_id:
                return entry
        return None

    def journal_pin(entry_id):
        entry = journal_entry(entry_id)
        entry["pinned"] = not entry.get("pinned")
        renpy.restart_interaction()

    def journal_remove(entry_id):
        store.game_state["journal"] = [e for e in store.game_state["journal"] if e["id"] != entry_id]
        renpy.restart_interaction()

    def journal_open_editor(entry_id):
        entry = journal_entry(entry_id)
        runtime.journal_edit = {"id": entry_id, "before": dict(entry)}
        renpy.show_screen("journal_edit", entry_id=entry_id)
        renpy.restart_interaction()

    def journal_close_editor(keep):
        edit = runtime.journal_edit
        renpy.hide_screen("journal_edit")
        entry = journal_entry(edit["id"])
        if entry is not None:
            if keep and entry["content"].strip():
                ## The keywords follow what the player wrote: a name they added must be able to bring the entry back.
                entry["who"] = aig_state.named_in(current_card(), entry["title"] + "\n" + entry["content"])
            else:
                entry.clear()
                entry.update(edit["before"])
        input_fields.clear()
        renpy.restart_interaction()

    def direct_scene(card, state, narration, once=None):
        """Works out who speaks in each paragraph and with what expression, and moves the characters
        to wherever the text left them.

        Within a turn (once is given) the director must answer, like every other part of the turn,
        and LLMError is raised if it cannot. For the card's opening, which is shown either way, a
        failure just leaves the text as plain narration with nobody moved."""
        paragraphs = aig_prompt.split_paragraphs(narration)
        if not card.characters:
            return aig_prompt.parse_direction("", card, len(paragraphs))
        ask = lambda: run_helper_sure("direct_scene", aig_prompt.director_prompt(card, state, paragraphs, prompts=active_preset()["prompts"]), "paragraphs")
        if once is not None:
            reply = once("direction", ask)
        else:
            try:
                reply = ask()
            except aig_llm.LLMError:
                reply = ""
        placed = aig_prompt.credible_whereabouts(card, state, narration, aig_prompt.parse_whereabouts(reply, card, state))
        ## Someone who speaks in the scene is in it, wherever the game had them and whether or not the
        ## text showed them coming, unless the director says outright that they are somewhere else.
        said_where = set(u["id"] for u in placed if "location" in u)
        here = state["actors"]["player"]["location"]
        for d in aig_prompt.parse_direction(reply, card, len(paragraphs)):
            who = d["speaker"]
            if who and who not in said_where and state["actors"][who]["location"] != here:
                placed.append({"id": who, "location": "here"})
                said_where.add(who)
        aig_state.track(card, state, placed)
        ## A line on how things stand, which the narrator is given back next turn. See describe_scene.
        state["scene"] = aig_prompt.parse_scene(reply)
        ## Places the text showed the player go on their map. The narrator hears of it next turn.
        for place in aig_state.reveal(card, state, aig_prompt.parse_revealed(reply, card, state)):
            state["pending_results"].append({"ok": True, "message": "The map now shows %s." % place})
        direction = aig_prompt.parse_direction(reply, card, len(paragraphs))
        ## Anyone who speaks in the scene is someone the player has now met.
        aig_state.meet(state, [d["speaker"] for d in direction if d["speaker"]])
        return direction

    def direct_opening():
        state = store.game_state
        set_busy(True)
        try:
            state["opening_direction"] = direct_scene(current_card(), state, aig_prompt.opening(current_card(), state))
        finally:
            set_busy(False)

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
        """The picture behind the scene: the one of the place the player is at, else the card's own, else none."""
        card = current_card()
        here = aig_state.places(card, store.game_state).get(store.game_state["actors"][aig_card.PLAYER]["location"])
        path = (here or {}).get("background") or card.data.get("display", {}).get("background")
        return card_image(path) if path else None

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

    PLAY_BAR_HEIGHT = 117
    CLOCK_LINE_HEIGHT = 34

    def clock_line():
        """The story's time and place line as it stands, for the bar at the top, or "" when the
        player has it switched off or the story has not given one yet. See prompt.split_header."""
        if not params().get("header", True) or not store.game_state:
            return ""
        line = (store.game_state.get("header") or aig_prompt.header_start(current_card(), store.game_state)).strip("[] ")
        return line if len(line) <= 180 else line[:177].rstrip() + "..."

    def clock_size():
        """A long line is set smaller so that it stays on its one row."""
        length = len(clock_line())
        return 24 if length <= 120 else 20 if length <= 145 else 17

    def play_bar_height():
        return PLAY_BAR_HEIGHT + (CLOCK_LINE_HEIGHT if clock_line() else 0)
    WAITING_HEIGHT = 110        # the strip a text-only card shows under its story while a reply is awaited

    def story_gap():
        """(top, height) of the space the play screen leaves for the story, in pixels, as last drawn.
        While the play screen is not up (a reply is awaited), everything under the bar."""
        layout = renpy.get_widget("turn_input", "play_layout")
        offsets = getattr(layout, "offsets", None)
        if offsets and len(offsets) >= 3:
            top, bottom = int(offsets[2][1]), int(offsets[1][1])       # children are laid out in the order t, b, c
            if bottom - top > 80:
                return top, bottom - top
        ## No input on screen: everything under the bar, less the strip that says a reply is on its way.
        waiting = WAITING_HEIGHT if renpy.get_screen("thinking") else 0
        return play_bar_height(), config.screen_height - play_bar_height() - waiting

    def story_awaiting():
        """What the player sent that is still being answered, or None. Shown under the story meanwhile."""
        if renpy.get_screen("thinking") is None:
            return None
        sent = getattr(store, "player_text", "") or ""
        return sent if sent and not sent.startswith("(") else ""

    def story_newest():
        """How far down the story the newest turn starts, in pixels, so that the view can open on it:
        the player's own line first, then the reply, read downwards. None if that cannot be told."""
        box = renpy.get_widget("story_panel", "story_log_box")
        offsets = getattr(box, "offsets", None)
        return offsets[-1][1] if offsets else None

    def story_layout(typed=""):
        """What decides how tall the input area of a text-only card is. When it changes, the story
        above has to fit itself to the new gap, which it can only measure once the input has been
        drawn; turn_input uses this to ask for one more look, once, and not by checking over and over."""
        return (len(store.suggestions), bool(runtime.suggesting), bool(store.turn_error), typed.count("\n"), len(typed) // 40, len(store.game_state["history"]))

    def story_refit(layout):
        runtime.story_layout = layout
        renpy.restart_interaction()

    def summarize_if_long(card, state, preset):
        """Folds the older half of the turns the model still sees into the summary. Skipped quietly if the model fails.

        The turns stay in history so the player can still read them.
        """
        limit = param_number("summarize_after_turns", 0, 1000, 50)
        ## Leave room in the context for the reply itself.
        budget = param_number("max_context_tokens", 2000, 2000000, 16000) - story_sampling()["max_tokens"]
        for attempt in range(3):
            seen = state["history"][state.get("summarized", 0):]
            too_many = limit and len(seen) > limit
            too_big = len(seen) > 2 and aig_llm.estimate_tokens(*aig_prompt.narrator_prompt(card, state, preset, "", [])) > budget
            if not (too_many or too_big):
                return
            old = seen[:max(1, len(seen) // 2)]
            try:
                state["summary"] = run_helper("summarize", aig_prompt.summary_prompt(card, state, old, prompts=preset["prompts"]))
            except aig_llm.LLMError:
                return
            state["summarized"] = state.get("summarized", 0) + len(old)

    ## Parameters: the player's generation settings.

    PARAM_SLIDERS = [
        ("temperature", _("Temperature"), 0.0, 2.0, 0.05, _("How adventurous the writing is. Higher is more varied and surprising, lower is steadier and more predictable.")),
        ("top_p", _("Top P"), 0.0, 1.0, 0.01, _("Keeps only the most likely words up to this share. Lower is safer and plainer. Usually left alone if you change temperature.")),
        ("top_k", _("Top K"), 0, 200, 1, _("Keeps only this many of the most likely words at each step. Not every provider accepts it.")),
        ("frequency_penalty", _("Frequency penalty"), -2.0, 2.0, 0.05, _("Above zero discourages repeating the same words often.")),
        ("presence_penalty", _("Presence penalty"), -2.0, 2.0, 0.05, _("Above zero nudges the model toward new subjects.")),
    ]
    PARAM_FALLBACKS = {"temperature": 1.0, "top_p": 1.0, "top_k": 40, "frequency_penalty": 0.0, "presence_penalty": 0.0}

    def reset_params():
        """Sets the player's parameters back to what the preset ships with."""
        sampling, context = active_preset().get("sampling", {}), active_preset().get("context", {})
        fresh = {"bookkeeper": True, "max_tokens": str(sampling.get("max_tokens", 2000)), "max_context_tokens": str(context.get("max_context_tokens", 16000)),
                 "summarize_after_turns": str(context.get("summarize_after_turns", 50)), "journal": True, "use": {}}
        for key, fallback in PARAM_FALLBACKS.items():
            fresh[key] = sampling.get(key, fallback)
            fresh["use"][key] = key in sampling
        persistent.params = fresh
        input_fields.clear()
        renpy.restart_interaction()

    def params():
        if persistent.params is None:
            reset_params()
        return persistent.params

    def flip(target, key, default, one=True, other=False):
        """Switches a setting between two values. Unlike Ren'Py's ToggleDict it copes with a setting
        that is not there yet, which is every setting added after the player's settings were first
        saved: default is what a missing one counts as."""
        target[key] = other if target.get(key, default) == one else one
        renpy.restart_interaction()

    def param_number(key, low, high, fallback):
        """A typed number from the Parameters screen, kept within sensible bounds. Nonsense falls back."""
        try:
            return max(low, min(high, int(str(params().get(key, "")).strip())))
        except ValueError:
            return fallback

    def bookkeeping():
        """Whether a bookkeeper records each reply's changes, instead of the story model reporting them itself."""
        return params().get("bookkeeper", True)

    def story_sampling():
        """What the story model is asked for. A setting the player has not switched on is left to the provider."""
        chosen = params()
        sampling = {"max_tokens": param_number("max_tokens", 50, 200000, 2000)}
        for key in PARAM_FALLBACKS:
            if chosen["use"].get(key):
                sampling[key] = chosen[key]
        return sampling

    def start_suggestions():
        """Fetches suggested replies in the background; the input screen fills in when they arrive."""
        store.suggestions = []
        conf = active_preset().get("suggestions", {})
        if not conf.get("enabled", True):
            return
        count, turn = conf.get("count", 3), store.game_state["turn"]
        prompt = aig_prompt.suggest_prompt(current_card(), store.game_state, count, prompts=active_preset()["prompts"])

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
            request = aig_llm.models_request(request_connection(slot))
        except aig_llm.LLMError as e:
            return set_status(runtime.models, slot, str(e))
        set_status(runtime.models, slot, "Loading models...")

        def done(result):
            if not isinstance(result, Exception):
                remember_contexts(slot, result)
            set_status(runtime.models, slot, str(result) if isinstance(result, Exception) else (aig_llm.model_ids(result) or "The provider listed no models."))

        run_in_background(lambda: http_json(request, 30, threaded=True), done)

    ## Context size that follows the model.

    def context_key(slot="main", model=None):
        connection = slot_connection(slot)
        return "%s|%s|%s" % (connection["provider"], (connection.get("base_url") or "").strip(), model or llm_settings()["models"][slot]["model"].strip())

    def remember_contexts(slot, listing):
        for model, tokens in aig_llm.model_contexts(listing).items():
            persistent.model_contexts[context_key(slot, model)] = tokens

    def sync_context_size(ask=True):
        """Sets Context size to the most the chosen story model can take, whenever that model changes.

        The limit comes from the provider's model list. If the list does not say (many custom and
        local endpoints do not), the size is left as it is and the Parameters screen says so.
        """
        chosen = params()
        key = context_key()
        if not chosen.get("auto_context", True) or key.endswith("|"):
            return
        known = persistent.model_contexts.get(key)
        if known:
            chosen["max_context_tokens"], chosen["context_for"], chosen["context_unknown"] = str(known), key, False
            renpy.restart_interaction()
        elif ask and chosen.get("context_for") != key and runtime.context_lookup != key:
            # Not seen in a model list yet (the id was typed in): ask the provider once, quietly.
            runtime.context_lookup = key
            try:
                request = aig_llm.models_request(request_connection("main"))
            except aig_llm.LLMError:
                return

            def done(result):
                if not isinstance(result, Exception):
                    remember_contexts("main", result)
                if context_key() == key:
                    if key in persistent.model_contexts:
                        sync_context_size(ask=False)
                    else:
                        ## A number that was set from another model's limit may be far more than this one can
                        ## take, so it is not kept. A number the player typed themselves is left alone.
                        if chosen.get("context_for") and not chosen.get("context_unknown"):
                            chosen["max_context_tokens"] = str(active_preset().get("context", {}).get("max_context_tokens", 16000))
                        chosen["context_for"], chosen["context_unknown"] = key, True
                        renpy.restart_interaction()

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


## What a text-only card shows behind its text: its background picture, darkened so the story
## stays easy to read, or plain black when the card has none. It is drawn by the screens that need
## it, not shown by the script, so a save made before a card had a picture still gets it.
screen backdrop():
    $ background = stage_background() if store.card_name and store.game_state else None
    add "#000000"
    if background:
        add background fit "cover" xysize (config.screen_width, config.screen_height)
        ## Only a light veil here: the panels the text sits in are already dark.
        add "#00000040"


screen thinking():
    if store.card_name and not current_card().visual:
        ## A text-only card keeps its story on screen while the reply is awaited, so the notice
        ## takes the place of the input, under the story, and nothing is laid over the text.
        ## The bar on top is shown without its buttons: nothing should be changed mid-turn.
        frame:
            xfill True
            ysize play_bar_height()
            padding (60, 20)
            background "#000000b0"
            vbox:
                ## The same heights as the real bar, whose first row is as tall as its buttons, so nothing shifts when the input comes back.
                fixed:
                    ysize 48
                    text esc(current_card().title) size 30 color "#cccccc" yalign 0.5
                text esc(status_line()) size 24 color "#cccccc"
                if clock_line():
                    text esc(clock_line()) size clock_size() color "#9fc4e8" layout "nobreak"
        frame:
            xfill True
            yalign 1.0
            ysize WAITING_HEIGHT
            padding (60, 0)
            background "#000000b8"
            vbox:
                yalign 0.5
                text esc("The story continues..." + (("   %d words so far" % runtime.wait_words) if runtime.wait_words else "")) color "#cccccc"
                if runtime.wait_note:
                    text esc(runtime.wait_note) size 22 color "#ffb070"
    else:
        frame:
            align (0.5, 0.35)
            padding (60, 40)
            vbox:
                text esc("The story continues..." + (("   %d words so far" % runtime.wait_words) if runtime.wait_words else ""))
                if runtime.wait_note:
                    text esc(runtime.wait_note) size 22 color "#ffb070"


## A turn as text: what the player said, each paragraph with its speaker, then what changed.
screen turn_text(turn=None):
    vbox:
        spacing 12
        if turn:
            text rich("> " + turn["player"]) color "#aaaaaa"
        ## A character's name heads their part once: what they do and what they say after it are
        ## theirs until someone else, or the narration, takes over.
        $ lines = scene_lines(turn)
        for n, (speaker, expression, paragraph) in enumerate(lines):
            if speaker and (n == 0 or lines[n - 1][0] != speaker):
                text esc(current_card().characters[speaker]["name"]) color speaker_color(speaker) size 26
            text rich(paragraph)
        ## A result the engine left to the story (ok is None) is a note for the narrator, not news for the player.
        for result in [r for r in (turn["results"] if turn else []) if r["ok"] is not None]:
            text esc(result["message"]) size 24 color ("#9fd89f" if result["ok"] else "#ff8080")
        if turn and turn.get("notice"):
            text esc(turn["notice"]) size 24 color "#ffb070"


## Where the player answers. The last response stays readable above the input unless the player hides it.
## Cards without assets get the story as a scrolling log on a plain background instead.
screen turn_input():
    default typed = draft
    $ card = current_card()

    if card.visual:
        use play_bar

        if recap_hidden:
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
                    viewport:
                        ysize 300
                        scrollbars "vertical"
                        mousewheel True
                        draggable True
                        use turn_text(game_state["history"][-1] if game_state["history"] else None)
                    use turn_controls(typed)

    else:
        ## A text-only card is all story, so the story takes the whole screen: the bar on top, the
        ## input at the bottom, and the text filling everything between.
        ##
        ## The story itself is not drawn here. It is drawn by the story_panel screen underneath,
        ## into the gap this layout leaves in the middle. Kept in this screen, every paragraph of
        ## the story was laid out and drawn again for each key the player pressed (a third of a
        ## second per letter with a long story); a screen of its own is drawn once and kept.
        side "t b c":
            id "play_layout"
            xfill True
            yfill True
            use play_bar
            frame:
                xfill True
                padding (60, 16, 60, 20)
                background "#000000b8"
                vbox:
                    spacing 12
                    use turn_controls(typed)
            ## The gap. It has to claim the space, or the layout closes up around nothing.
            fixed:
                xfill True
                yfill True
        ## The input area has changed height (suggestions came, a line was added): once it has been
        ## drawn, have the story above fit itself to the new gap. Once per change; nothing runs in between.
        if story_layout(typed) != runtime.story_layout:
            timer 0.05 action Function(story_refit, story_layout(typed))


## The story of a text-only card, with the card's picture behind it. Shown by the script for as
## long as such a card is being played, so it also stays up while a reply is awaited. It fits
## itself into the gap the play screen leaves between its bar and its input.
screen story_panel():
    zorder -5
    default story_scroll = ui.adjustment()
    $ top, height = story_gap()
    ## What the player has just sent, while its reply is awaited. It is not part of the story yet.
    $ sent = story_awaiting()

    use backdrop
    frame:
        xfill True
        ypos top
        ysize height
        padding (60, 18, 40, 10)
        background "#000000b8"
        hbox:
            style_prefix "story"
            spacing 12
            storyview:
                adjustment story_scroll
                ## Where the view settles each time it is drawn afresh: on the newest turn, or, while a
                ## reply is awaited, at the very end, on what was last read and what was just sent.
                where (None if sent is not None else story_newest)
                version (len(game_state["history"]), game_state.get("summarized", 0), top, height, sent)
                xsize 1786
                vbox:
                    spacing 30
                    use story_log(TEXT_MODE_TURNS)
                    if sent:
                        text rich("> " + sent) color "#aaaaaa"
            vbar adjustment story_scroll style "vscrollbar" yfill True



## The bar across the top of the play screen: the card, the buttons, and how the player stands.
screen play_bar():
    $ card = current_card()
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
            ## The story's own clock: time, date, exact spot and weather, as the story model last gave them.
            if clock_line():
                text esc(clock_line()) size clock_size() color "#9fc4e8" layout "nobreak"


## What sits under the story on the play screen: an error if the last turn failed, the suggested
## replies, the box to type in, and the small row of hints. typed is what is in the box right now:
## the box itself edits turn_input's own variable of that name, and Send returns it.
screen turn_controls(typed):
    $ card = current_card()

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
        if game_state["history"] and can_undo():
            textbutton _("Undo last turn") action Function(undo_turn) text_size 22 yalign 0.5
        text _("*italic*  **bold**") size 22 color "#999999" yalign 0.5
        if usage_brief():
            text esc(usage_brief()) size 22 color "#999999" yalign 0.5


## Story text over a picture: a dark edge around every letter.
style story_text is text
style story_text:
    outlines [(3, "#000000c8", 0, 0)]
style story_vscrollbar is vscrollbar


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


## Preset: what the story model is told, as a list of parts that can be switched, edited and reordered.

screen preset():
    tag menu
    default show_builtin = False
    default show_wording = False

    use game_menu(_("Preset"), scroll="viewport"):
        $ blocks = active_preset()["blocks"]
        $ files = preset_files()

        vbox:
            spacing 18
            text _("What the story model is told before it writes. Each line is one instruction: tick it to use it, or edit its wording. Changes are yours, on this device, for every card.") size 24

            vbox:
                spacing 4
                text _("Presets are files in the game's presets folder. Make them in the card creator, under Presets, or import one somebody shared.") size 22 color "#999999"
                hbox:
                    spacing 30
                    textbutton _("Import a preset file") action Function(preset_import)
                    if runtime.preset_message:
                        text esc(runtime.preset_message) size 22 color "#ffd28a" yalign 0.5
                if len(files) > 1:
                    hbox:
                        spacing 30
                        box_wrap True
                        style_prefix "radio"
                        for name, title in files:
                            if preset_changed() and name != (persistent.preset_file or "default"):
                                textbutton esc(title) action Confirm(_("Switch preset? The changes you made here to the current one will be removed."), Function(preset_choose, name)) selected False
                            else:
                                textbutton esc(title) action Function(preset_choose, name) selected (name == (persistent.preset_file or "default"))

            hbox:
                spacing 30
                vbox:
                    style_prefix "check"
                    xsize 700
                    textbutton _("Also show the built-in parts") action ToggleScreenVariable("show_builtin")
                if preset_changed():
                    textbutton _("Reset to the original") action Confirm(_("Put the preset back as it came? Your own instructions and edits will be removed."), Function(reset_preset)) yalign 0.5

            for index, block in enumerate(blocks):
                $ builtin = block["kind"] == "slot"
                $ fixed = block.get("slot") == "action_protocol"
                if show_builtin or not builtin:
                    vbox:
                        spacing 2
                        hbox:
                            spacing 18
                            vbox:
                                style_prefix "check"
                                xsize 760
                                textbutton esc(block["name"] + ("   (built in)" if builtin else "")) action Function(preset_toggle, index) selected (fixed or block.get("enabled", True)) sensitive (not fixed)
                            if not builtin:
                                textbutton _("Edit") action Function(preset_open_editor, index) text_size 26 yalign 0.5
                            textbutton _("Up") action Function(preset_move, index, -1) text_size 26 yalign 0.5 sensitive (index > 0)
                            textbutton _("Down") action Function(preset_move, index, 1) text_size 26 yalign 0.5 sensitive (index < len(blocks) - 1)
                            if not builtin:
                                textbutton _("Remove") action Confirm(_("Remove this instruction?"), Function(preset_delete, index)) text_size 26 yalign 0.5
                        if builtin:
                            text preset_note(block) size 22 color "#999999"
                        else:
                            text esc((block.get("content") or "(empty: press Edit to write it)")) size 22 color ("#cccccc" if block.get("enabled", True) else "#777777")
                elif block.get("slot") == "history":
                    text _("- - -  the story so far goes here  - - -") size 22 color "#999999"

            hbox:
                spacing 40
                textbutton _("Add an instruction") action Function(preset_add_and_edit, False)
                textbutton _("Add a reminder after the story") action Function(preset_add_and_edit, True)

            null height 10
            vbox:
                style_prefix "check"
                xsize 900
                textbutton _("Show the game's own prompts (advanced)") action ToggleScreenVariable("show_wording")
            if show_wording:
                text _("The wording the game itself uses: how it explains the rules to the story model, and the whole prompt of each helper job. Reword one to suit a model that keeps getting something wrong. Reset puts the original back.") size 22 color "#999999"
                for reader, heading in (("story", _("Told to the story model")), ("helper", _("Helper jobs"))):
                    label heading
                    for entry in aig_wording.BUILTIN_PROMPTS:
                        if entry["reader"] == reader:
                            vbox:
                                spacing 2
                                hbox:
                                    spacing 18
                                    text esc(entry["title"] + ("  (reworded)" if wording_is_own(entry["key"]) else "")) xsize 760 yalign 0.5 color ("#ffd28a" if wording_is_own(entry["key"]) else "#ffffff")
                                    textbutton _("Edit") action Function(wording_open_editor, entry["key"]) text_size 26 yalign 0.5
                                    if wording_is_own(entry["key"]):
                                        textbutton _("Reset") action Confirm(_("Put this prompt back to its original wording?"), Function(wording_reset, entry["key"])) text_size 26 yalign 0.5
                                text esc(entry["help"]) size 22 color "#999999"

            text _("Instructions above the story are sent once and cached, so they cost little. A reminder after the story is sent after your newest message, as the last thing the model reads: it costs a few tokens each turn, but models pay it the most attention. The order matters most among the instructions themselves; the game always sends its changing parts (state, quests, lore) with the newest message.") size 22 color "#999999"


## Editing one journal entry.
screen journal_edit(entry_id):
    modal True
    zorder 30
    $ entry = journal_entry(entry_id)

    add "#000000c0"
    if entry is not None:
        frame:
            align (0.5, 0.5)
            xsize 1500
            ysize 760
            padding (40, 30)

            vbox:
                spacing 16
                label _("Journal entry")
                use settings_input(_("Title"), entry, "title")
                text _("What the story model is reminded of. Correct anything it got wrong, and add what must not be forgotten. Names you write here also bring the entry back when those people are around.") size 22 color "#999999"
                button:
                    xfill True
                    ysize 380
                    padding (16, 12)
                    background "#00000080"
                    action settings_field(entry, "content").Toggle()
                    viewport:
                        scrollbars "vertical"
                        mousewheel True
                        input value settings_field(entry, "content") multiline True copypaste True xmaximum 1340

            hbox:
                align (1.0, 1.0)
                spacing 40
                textbutton _("Cancel") action [DisableAllInputValues(), Function(journal_close_editor, False)]
                textbutton _("Done") action [DisableAllInputValues(), Function(journal_close_editor, True)]


## Editing one of the game's own prompts. Like preset_edit, it only reads.
screen wording_edit(key):
    modal True
    zorder 20
    $ entry = aig_wording.builtin_prompt(key)
    $ mine = (persistent.preset or {}).get("prompts") or {}

    add "#000000c0"
    if key in mine:
        frame:
            align (0.5, 0.5)
            xsize 1500
            ysize 960
            padding (40, 30)

            vbox:
                spacing 12
                label esc(entry["title"])
                text esc(entry["help"]) size 22 color "#999999"
                if entry["parts"]:
                    text esc("The game fills these in; keep them where they should appear: " + "; ".join("{{%s}} is %s" % (name, what) for name, what in sorted(entry["parts"].items()))) size 22 color "#ffd28a"
                if "JSON" in entry["text"]:
                    text _("Keep the part that says how to reply (the JSON shape). The game reads the reply by that shape.") size 22 color "#ffd28a"
                text esc("EDITABLE  " + entry["where"]) size 22 color "#7fd18b"
                button:
                    xfill True
                    ysize (430 if entry["sends"] else 520)
                    padding (16, 12)
                    background "#00000080"
                    action settings_field(mine, key).Toggle()
                    viewport:
                        scrollbars "vertical"
                        mousewheel True
                        input value settings_field(mine, key) multiline True copypaste True xmaximum 1340
                if entry["sends"]:
                    text esc("BUILT IN  Sent under it, in this order, written by the game each time: " + "   ".join(section["heading"] for section in entry["sends"])) size 22 color "#999999"

            hbox:
                align (1.0, 1.0)
                spacing 40
                textbutton _("Cancel") action Function(wording_close, False)
                textbutton _("Done") action Function(wording_close, True)


## Editing one instruction. It must only read: Ren'Py draws screens ahead of time to have them
## ready, so anything a screen changed while being drawn would happen without the player asking.
screen preset_edit(index):
    modal True
    zorder 20
    $ blocks = active_preset()["blocks"]
    ## Empty if the instruction is gone, so the screen can never fail while closing.
    $ block = blocks[index] if index < len(blocks) else None

    add "#000000c0"
    if block is not None:
        frame:
            align (0.5, 0.5)
            xsize 1500
            ysize 900
            padding (40, 30)

            vbox:
                spacing 16
                label _("Instruction")
                use settings_input(_("Name"), block, "name")
                text _("What the story model is told. Write it as a plain instruction. Use {{{{user}} for the player's name. Click the box to type; paste works.") size 22 color "#999999"
                button:
                    xfill True
                    ysize 520
                    padding (16, 12)
                    background "#00000080"
                    action settings_field(block, "content").Toggle()
                    viewport:
                        scrollbars "vertical"
                        mousewheel True
                        input value settings_field(block, "content") multiline True copypaste True xmaximum 1340

            hbox:
                align (1.0, 1.0)
                spacing 40
                textbutton _("Cancel") action Function(preset_cancel_edit)
                textbutton _("Done") action Hide("preset_edit")


## Parameters.

screen parameters():
    tag menu

    on "show" action Function(sync_context_size)

    use game_menu(_("Parameters"), scroll="viewport"):
        $ chosen = params()

        vbox:
            spacing 26
            text _("How the story model writes. These are yours: they apply on this device, to every card.") size 24

            vbox:
                spacing 6
                label _("Record-keeping")
                text _("Who updates health, mana, items, places, states and quests after each reply. With the bookkeeper on, the story model only writes the story, and a second call (the \"Keep the books\" small task) reads it and records what changed. This is much more dependable with smaller story models, at the cost of one more small call per turn. With it off, the story model must report its own changes, which the strongest models do well and others often forget.") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Use a bookkeeper") action Function(flip, chosen, "bookkeeper", True) selected chosen.get("bookkeeper", True)

            vbox:
                spacing 6
                label _("Check before writing")
                text _("For a story model that thinks before it answers. It is given a short checklist to go through in its thinking first: does the reply fit the game state and this turn's results, does it follow the card's and the preset's instructions, is it laid out right. Replies take a little longer and cost a little more. A model that cannot think privately writes the check out instead, which the game removes, so leave this off for those. The checklist can be reworded in the preset (Check before writing).") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Have the story model check its reply first") action Function(flip, chosen, "check_first", False, True, False) selected chosen.get("check_first", False)

            vbox:
                spacing 6
                label _("Time and place")
                text _("The story keeps its own clock: the hour, the date, the exact spot and the weather, moved on by the story model as things happen and shown in the bar at the top. People in the story feel the hour and the weather. A card can give the line its own form, such as its world's calendar; otherwise it is an ordinary clock and date. Off, no line is asked for or shown.") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Keep a time and place line") action Function(flip, chosen, "header", True) selected chosen.get("header", True)

            vbox:
                spacing 6
                label _("The world moves on")
                text _("When you go somewhere else, and now and then when you stay put, a helper (the \"Move the world on\" small task) works out where the people you know have got to in the meantime, so nobody stays for ever where the story last showed them. It costs one small call each time. Off, people stay where they were last seen until the story shows them elsewhere.") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Let people move while I am elsewhere") action Function(flip, chosen, "world", True) selected chosen.get("world", True)

            vbox:
                spacing 6
                label _("Journal")
                text _("At the end of each scene (you go somewhere else, an objective is finished, a fight is over) a helper writes a short entry about it. Much later, when that scene has long left what the story model is sent, the entry is sent again on the turns where it matters: when its subject comes up, when you are back there, or when someone from it is with you. It costs one small call per scene, and nothing on the turns where no entry applies. Read and correct the entries under Journal on the play screen.") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Keep a journal") action Function(flip, chosen, "journal", True) selected chosen.get("journal", True)

            vbox:
                spacing 6
                label _("Response length")
                text _("The most the model may write in one reply, in tokens (a token is about three quarters of a word). If replies stop mid-sentence, raise this. Models that think before answering spend part of it on thinking, so give those 3000 or more.") size 24
                use settings_input(_("Tokens"), chosen, "max_tokens", digits=True)

            vbox:
                spacing 6
                label _("Context size")
                text _("How much the model is sent each turn, in tokens: the card, the story so far and the game state. When the story outgrows it, the oldest turns are folded into a summary. Set it to what your model can take; larger remembers more and costs more.") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Match the most the story model can take") action [Function(flip, chosen, "auto_context", True), Function(sync_context_size)] selected chosen.get("auto_context", True)
                use settings_input(_("Tokens"), chosen, "max_context_tokens", digits=True)
                if chosen.get("auto_context", True):
                    if chosen.get("context_unknown") and chosen.get("context_for") == context_key():
                        text _("This provider does not say how much this model can take, so the number above is a cautious default. If you know the model's limit, type it in.") size 22 color "#ffb070"
                    elif chosen.get("context_for") == context_key():
                        text esc("Set to the limit of %s. It follows the model whenever you choose a different one." % context_key().split("|")[-1]) size 22 color "#999999"
                    else:
                        text _("It will be set from the provider's model list when you choose a story model.") size 22 color "#999999"
                use settings_input(_("Turns"), chosen, "summarize_after_turns", digits=True)
                text _("Also summarize once this many turns have built up, whichever comes first. 0 goes by size only.") size 22 color "#999999"

            for key, title, low, high, step, help in PARAM_SLIDERS:
                vbox:
                    spacing 6
                    label title
                    text help size 24
                    vbox:
                        style_prefix "check"
                        xsize 1100
                        textbutton _("Set it myself") action Function(flip, chosen["use"], key, False)
                    if chosen["use"].get(key):
                        hbox:
                            spacing 24
                            bar value DictValue(chosen, key, range=high - low, offset=low, step=step) xsize 700 yalign 0.5
                            text (("%d" if isinstance(step, int) else "%.2f") % chosen[key]) yalign 0.5
                    else:
                        text _("Left to the provider's default.") size 24 color "#999999"

            vbox:
                spacing 6
                text _("With Anthropic chosen directly as the provider, temperature, Top P and Top K are not sent, because current Claude models reject them, and the response length is never set below 16,000 so that thinking does not use it all up.") size 22 color "#999999"
                textbutton _("Reset to the preset's values") action Function(reset_params)


## Models.

screen models():
    tag menu

    ## A model id typed by hand is noticed when the player leaves this screen.
    ## Leaving the page also lets go of whichever box was being typed in, so later typing cannot land in it.
    on "hide" action [DisableAllInputValues(), Function(sync_context_size), Function(keep_settings)]
    on "replaced" action [DisableAllInputValues(), Function(sync_context_size), Function(keep_settings)]

    use game_menu(_("Models"), scroll="viewport"):
        $ settings = llm_settings()
        $ utility = settings["models"]["utility"]

        vbox:
            spacing 30

            use model_slot("main", _("Main model"), _("Writes the story."))

            vbox:
                spacing 6
                label _("Token use")
                for line in usage_lines():
                    text esc(line) size 24
                if not usage_lines():
                    text _("Nothing yet. This fills in once the story model has replied, and starts again each time the game is launched.") size 24
                else:
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
                            if task not in dict(MEMORY_TASKS):
                                textbutton title action Function(flip, settings["tasks"], task, "utility", "main", "utility") selected (settings["tasks"].get(task, "utility") == "utility")

            ## The story's memory: whether a journal is kept, and which model writes it and the summary.
            $ wants_memory_model = any(settings["tasks"].get(task) == "memory" for task, title in MEMORY_TASKS)
            vbox:
                spacing 10
                label _("Memory")
                text _("How the story is remembered once it is too long to send whole. The summary is one short recap of everything so far. The journal keeps an entry for each scene and reminds the story model of the ones that matter. Each can be written by the main model, by the model for small tasks, or by a model of its own.") size 24
                vbox:
                    style_prefix "check"
                    xsize 1100
                    textbutton _("Keep a journal") action Function(flip, params(), "journal", True) selected params().get("journal", True)
                for task, title in MEMORY_TASKS:
                    if task != "write_journal" or params().get("journal", True):
                        hbox:
                            spacing 20
                            text title yalign 0.5 min_width 420
                            hbox:
                                style_prefix "radio"
                                spacing 10
                                textbutton _("Main model") action SetDict(settings["tasks"], task, "main") selected (settings["tasks"].get(task, "utility") == "main" or (settings["tasks"].get(task, "utility") == "utility" and not utility["enabled"]))
                                if utility["enabled"]:
                                    textbutton _("Small-tasks model") action SetDict(settings["tasks"], task, "utility") selected (settings["tasks"].get(task, "utility") == "utility")
                                textbutton _("Its own model") action SetDict(settings["tasks"], task, "memory") selected (settings["tasks"].get(task) == "memory")
                if wants_memory_model:
                    use model_slot("memory", _("Memory model"), _("Used for whichever of the two above is set to its own model."))
                    if not settings["models"]["memory"]["model"].strip():
                        text _("No model is chosen yet, so those jobs still run on the other models for now.") size 22 color "#ffb070"


screen model_slot(slot, title, help):
    $ settings = llm_settings()
    $ entry = settings["models"][slot]
    $ connection = slot_connection(slot)
    $ suggested = aig_llm.SUGGESTED_MODELS.get(connection["provider"], {}).get(slot)

    vbox:
        spacing 10
        label title
        text help size 24

        if slot != "main":
            vbox:
                style_prefix "check"
                xsize 1100
                textbutton _("Same provider and key as the main model") action ToggleDict(entry, "connection", "main", slot)

        if entry["connection"] == slot:
            hbox:
                style_prefix "radio"
                spacing 20
                for provider, name in aig_llm.PROVIDERS:
                    textbutton name action SetDict(connection, "provider", provider)

            use settings_input(_("API key"), runtime.keys, connection["id"], secret=True)
            text esc(aig_keystore.describe()) size 22 color "#999999"
            use settings_input(_("Address"), connection, "base_url", hint=aig_llm.DEFAULT_BASE_URLS[connection["provider"]])

        use settings_input(_("Model"), entry, "model", hint=suggested)

        hbox:
            spacing 30
            if suggested and entry["model"] != suggested:
                textbutton _("Use [suggested]") action SetDict(entry, "model", suggested)
            textbutton _("Choose from list") action [DisableAllInputValues(), Function(load_models, slot), Show("model_picker", slot=slot)]
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

    ## Typing goes to whichever box was clicked last. If that was a box on the page underneath, the
    ## search here would get nothing, so every other box is let go of when this opens, which hands
    ## the keyboard to the search.
    on "show" action DisableAllInputValues()

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
                                action [DisableAllInputValues(), SetDict(entry, "model", model), Function(sync_context_size), Function(keep_settings), Hide("model_picker")]
                                selected (entry["model"] == model)
                                text_size 26

        textbutton _("Close") action [DisableAllInputValues(), Hide("model_picker")] xalign 1.0 yalign 1.0


screen settings_input(title, target, key, secret=False, hint=None, digits=False):
    $ field = settings_field(target, key)
    hbox:
        spacing 20
        text title yalign 0.5 min_width 180
        button:
            xsize (300 if digits else 900)
            padding (16, 10)
            background "#00000080"
            action field.Toggle()
            if secret:
                input value field mask "*" copypaste True
            elif digits:
                input value field allow "0123456789" length 7
            else:
                input value field copypaste True
        if hint and not target[key]:
            text esc(hint) size 24 color "#888888" yalign 0.5
