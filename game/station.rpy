## The console shell around a card: launch logo, inserting a card, taking it out.

## File or folder name, inside cards/, of the card that is inserted. Remembered between launches.
default persistent.card = None

transform logo_slide:
    xalign 0.5
    yalign 0.45
    xoffset -500
    alpha 0.0
    easeout 0.9 xoffset 0 alpha 1.0

transform tagline_fade:
    xalign 0.5
    yalign 0.58
    alpha 0.0
    pause 0.8
    linear 0.6 alpha 1.0

screen station_logo():
    add "#ffffff"
    text "SI-Station" size 130 color "#111111" at logo_slide
    text "Superintelligence Station" size 34 color "#777777" at tagline_fade

## Ren'Py runs this once at launch, before the main menu.
label splashscreen:
    ## Started by the "SI-Station Creator" launcher: only the creator, until the player chooses to play.
    if creator_only():
        $ open_creator()
        call screen creator_window
    show screen station_logo
    $ renpy.pause(2.6)
    hide screen station_logo
    return

## Ren'Py runs this every time the main menu is about to appear. No card, no menu.
label before_main_menu:
    while inserted_card() is None:
        call screen insert_card
        $ persistent.card = _return
    $ _window_subtitle = " - " + inserted_card().title
    return

## Ren'Py runs this right after a save is loaded.
label after_load:
    $ load_problem, load_notes = check_loaded_game()
    if selftest_report_path():
        $ selftest_after_load(load_problem)
    if load_problem:
        $ renpy.say(None, esc(load_problem))
        $ renpy.full_restart()
    if load_notes:
        $ renpy.say(None, esc("The card has changed since this save was made. " + " ".join(load_notes[:6])))
    ## A text-only card's story is drawn by its own screen. A save from before that screen existed does not have it up.
    if not load_problem and not current_card().visual:
        show screen story_panel
    return

default persistent.played = None

init python:
    import datetime
    import os
    import re
    import subprocess
    import time

    ## Saves are kept per card: the slot names carry the card's id, so the save and load screens
    ## only ever list the inserted card's saves.
    def card_save_prefix():
        card = inserted_card()
        return card.id + "-" if card else ""

    config.file_slotname_callback = lambda page, name: "%s%s-%s" % (card_save_prefix(), page, name)
    config.autosave_prefix_callback = lambda: card_save_prefix() + "auto-"

    def check_loaded_game():
        """Fits a loaded save to its card. Returns (why it cannot be played or None, what was dropped to make it fit)."""
        try:
            card = current_card()
        except aig_card.CardError:
            ## A save remembers the name its card had in the library, and the same card can be there
            ## under another name: a folder while it was being made, a .sicard file once exported and
            ## imported. What identifies a card is its id, so look for it by that.
            name = card_in_library(store.game_state.get("card_id"))
            if name is None:
                return "This save belongs to a card that is no longer in the library (%s). Insert that card to play it." % store.card_name, []
            store.card_name = name
            card = current_card()
        if card.id != store.game_state.get("card_id"):
            return "This save was made with a different card and cannot be played with this one.", []
        return None, aig_state.reconcile(card, store.game_state)

    def card_in_library(card_id):
        """The library name of the card with this id, or None. The inserted card is preferred if it is the one."""
        inserted = inserted_card()
        if inserted is not None and inserted.id == card_id:
            return persistent.card
        for entry in aig_card.list_cards(cards_dir()):
            if entry["card"] is not None and entry["card"].id == card_id:
                return entry["name"]
        return None

    def inserted_card():
        """The inserted card, or None if there is none or it no longer loads."""
        if not persistent.card:
            return None
        try:
            return aig_card.get_card(os.path.join(cards_dir(), persistent.card))
        except aig_card.CardError:
            return None

    def eject_card():
        persistent.card = None
        renpy.full_restart()

    def pick_file(prompt="Choose a card file", kinds="SI-Station cards (*.sicard)|*.sicard|"):
        """Opens the system's file chooser. Returns a path, None if cancelled, or raises OSError if there is no chooser.
        prompt is a short plain title; kinds is the Windows file filter that comes before "All files"."""
        if renpy.macintosh:
            command = ["osascript", "-e", 'POSIX path of (choose file with prompt "%s")' % prompt]
            flags = 0
        elif renpy.windows:
            script = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; "
                      "$d.Filter = '" + kinds + "All files (*.*)|*.*'; "
                      "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.FileName }")
            command = ["powershell", "-NoProfile", "-Command", script]
            flags = 0x08000000  # no console window
        elif renpy.linux:
            command = ["zenity", "--file-selection", "--title=" + prompt]
            flags = 0
        else:
            raise OSError("no file chooser on this platform")
        done = subprocess.run(command, capture_output=True, text=True, creationflags=flags) if flags else subprocess.run(command, capture_output=True, text=True)
        return done.stdout.strip() or None

    def import_card_file(path):
        """Adds the chosen card file to the library, in place of any copy already there. The player
        stays in the library, where the card now is, and chooses what to play."""
        try:
            name = aig_card.import_card(path, cards_dir())
            runtime.import_message = None
            runtime.import_note = "Added %s." % aig_card.get_card(os.path.join(cards_dir(), name)).title
        except (aig_card.CardError, IOError, OSError) as e:
            runtime.import_note = None
            runtime.import_message = "Could not import the card: %s" % (e.problems[0] if isinstance(e, aig_card.CardError) else e)
        renpy.restart_interaction()

    def import_from_file():
        """Asks for a card file and adds it to the library."""
        runtime.import_note = None
        try:
            path = pick_file()
        except Exception:
            runtime.import_message = "No file chooser is available here. Copy the card file into the folder below instead."
            return
        if not path:
            return
        try:
            ## A library keeps one copy of a card, so importing one that is already here replaces it.
            ## The player is asked first; what is replaced goes to the cards folder's .trash.
            title, already = aig_card.copies_of(path, cards_dir())
        except aig_card.CardError as e:
            runtime.import_message = "That file is not a usable card: " + e.problems[0]
            renpy.restart_interaction()
            return
        if already:
            renpy.show_screen("confirm", message="%s is already in your library.\nReplace it?" % esc(title),
                              yes_action=[Hide("confirm"), Function(import_card_file, path)], no_action=Hide("confirm"))
            renpy.restart_interaction()
        else:
            import_card_file(path)

    ## When each card was last played, for the library to put the latest first and say so. A card's
    ## newest save tells it (they are named after the card), and so does a note the game keeps for
    ## itself, for a player who saves nothing.

    def mark_played():
        if store.game_state and current_card() is not None:
            played = dict(persistent.played or {})
            played[current_card().id] = time.time()
            persistent.played = played

    def last_played(card):
        """The time the card was last played, or None if it never was."""
        times = [(persistent.played or {}).get(card.id)]
        try:
            times += [renpy.slot_mtime(slot) for slot in renpy.list_slots("^" + re.escape(card.id + "-"))]
        except Exception:
            pass
        times = [t for t in times if t]
        return max(times) if times else None

    def played_words(when):
        if not when:
            return "Not played yet"
        days = (datetime.date.today() - datetime.date.fromtimestamp(when)).days
        clock = time.strftime("%H:%M", time.localtime(when))
        return "Last played today, %s" % clock if days <= 0 else "Last played yesterday, %s" % clock if days == 1 else "Last played %s" % time.strftime("%d %b %Y", time.localtime(when)).lstrip("0")

    def card_icon(card):
        """The picture shown beside a card in the library: the card's own, or a plain disc."""
        own = card.data["meta"].get("icon")
        return (asset_image(card, own) if own else None) or "gui/card_disc.png"

    def library_cards():
        """The library as it is listed: the card played last first, cards never played after those
        that were, by title, and files that cannot be used at the end. Each usable entry has "played"."""
        entries = aig_card.list_cards(cards_dir())
        for entry in entries:
            entry["played"] = last_played(entry["card"]) if entry["card"] else None
        return sorted(entries, key=lambda e: (e["card"] is None, -(e["played"] or 0), (e["card"].title if e["card"] else e["name"]).lower()))


## What the inserted card is, and what its creator wants players to know.
screen card_info():
    tag menu

    use game_menu(_("Card info"), scroll="viewport"):
        $ card = inserted_card()
        if card:
            $ meta = card.data["meta"]
            $ counts = [(len(card.characters), "character"), (len(card.locations), "location"), (len(card.items), "item"), (len(card.quests), "quest")]
            vbox:
                spacing 14
                label esc(meta["title"])
                text esc("  |  ".join(([("by " + meta["author"])] if meta.get("author") else []) + ([("version " + meta["version"])] if meta.get("version") else []))) size 26
                if meta.get("tags"):
                    text esc("Tags: " + ", ".join(meta["tags"])) size 26
                if meta.get("description"):
                    text esc(meta["description"])
                text esc(", ".join("%d %s%s" % (n, word, "" if n == 1 else "s") for n, word in counts if n) + (". Plays with backgrounds and sprites." if card.visual else ". Plays as text only.")) size 26

                null height 20
                label _("Creator's note")
                text esc(meta.get("creator_note") or "The creator did not leave a note.")
        else:
            text _("No card is inserted.")


## Shown instead of the main menu while no card is inserted.
screen insert_card():
    default choosing = False
    add "#ffffff"
    ## The library is what the player comes for. Being asked to insert a card is only for someone
    ## who has none yet.
    $ library = library_cards()

    if not choosing and not library:
        vbox:
            align (0.5, 0.5)
            spacing 40
            text "SI-Station" size 40 color "#999999" xalign 0.5
            text _("Please insert a card") size 76 color "#111111" xalign 0.5
            textbutton _("Choose"):
                xalign 0.5
                text_size 48
                text_idle_color "#0077aa"
                text_hover_color "#00aaff"
                action SetScreenVariable("choosing", True)

    else:
        vbox:
            xalign 0.5
            ypos 80
            xsize 1300
            spacing 24
            text _("Choose a card") size 60 color "#111111"

            viewport:
                ysize 620
                scrollbars "vertical"
                mousewheel True
                draggable True
                vbox:
                    spacing 16
                    for entry in library:
                        if entry["card"]:
                            button:
                                xfill True
                                padding (24, 16)
                                background "#f0f0f0"
                                hover_background "#d8ecf6"
                                action Return(entry["name"])
                                hbox:
                                    spacing 24
                                    add card_icon(entry["card"]) fit "contain" xysize (120, 120) yalign 0.0
                                    vbox:
                                        xsize 1080
                                        $ meta = entry["card"].data["meta"]
                                        text esc(meta["title"]) size 40 color "#111111"
                                        text esc(played_words(entry["played"])) size 22 color ("#0077aa" if entry["played"] else "#999999")
                                        if meta.get("author"):
                                            text esc("by " + meta["author"]) size 24 color "#777777"
                                        if meta.get("description"):
                                            text esc(meta["description"]) size 26 color "#444444"
                        else:
                            frame:
                                xfill True
                                padding (24, 16)
                                background "#f6eaea"
                                vbox:
                                    text esc(entry["name"]) size 30 color "#884444"
                                    text esc("Cannot be used: " + entry["error"]) size 24 color "#884444"
                    if not library:
                        text _("No cards here yet.") color "#777777"

            if getattr(runtime, "import_message", None):
                text esc(runtime.import_message) size 26 color "#aa3333"
            elif getattr(runtime, "import_note", None):
                text esc(runtime.import_note) size 26 color "#227744"

            hbox:
                spacing 60
                textbutton _("Import a card file...") action Function(import_from_file) text_idle_color "#0077aa" text_hover_color "#00aaff"
                if not library:
                    textbutton _("Back") action SetScreenVariable("choosing", False) text_idle_color "#777777" text_hover_color "#00aaff"

            text esc("Cards are kept in " + cards_dir()) size 22 color "#999999"
