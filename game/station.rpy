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
    if load_problem:
        $ renpy.say(None, esc(load_problem))
        $ renpy.full_restart()
    if load_notes:
        $ renpy.say(None, esc("The card has changed since this save was made. " + " ".join(load_notes[:6])))
    return

init python:
    import subprocess

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
            return "This save belongs to a card that is no longer in the library (%s). Insert that card to play it." % store.card_name, []
        if card.id != store.game_state.get("card_id"):
            return "This save was made with a different card and cannot be played with this one.", []
        return None, aig_state.reconcile(card, store.game_state)

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

    def pick_file():
        """Opens the system's file chooser. Returns a path, None if cancelled, or raises OSError if there is no chooser."""
        if renpy.macintosh:
            command = ["osascript", "-e", 'POSIX path of (choose file with prompt "Choose a card file")']
            flags = 0
        elif renpy.windows:
            script = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; "
                      "$d.Filter = 'SI-Station cards (*.sicard)|*.sicard|All files (*.*)|*.*'; "
                      "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.FileName }")
            command = ["powershell", "-NoProfile", "-Command", script]
            flags = 0x08000000  # no console window
        elif renpy.linux:
            command = ["zenity", "--file-selection", "--title=Choose a card file"]
            flags = 0
        else:
            raise OSError("no file chooser on this platform")
        done = subprocess.run(command, capture_output=True, text=True, creationflags=flags) if flags else subprocess.run(command, capture_output=True, text=True)
        return done.stdout.strip() or None

    def import_from_file():
        """Asks for a card file and adds it to the library. Returns its library name, which inserts it, or None."""
        try:
            path = pick_file()
        except Exception:
            runtime.import_message = "No file chooser is available here. Copy the card file into the folder below instead."
            return None
        if not path:
            return None
        try:
            return aig_card.import_card(path, cards_dir())
        except aig_card.CardError as e:
            runtime.import_message = "That file is not a usable card: " + e.problems[0]
        except (IOError, OSError) as e:
            runtime.import_message = "Could not copy the card: %s" % e
        renpy.restart_interaction()
        return None


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

    if not choosing:
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
        $ library = aig_card.list_cards(cards_dir())
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
                                vbox:
                                    $ meta = entry["card"].data["meta"]
                                    text esc(meta["title"]) size 40 color "#111111"
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

            hbox:
                spacing 60
                textbutton _("Import a card file...") action Function(import_from_file) text_idle_color "#0077aa" text_hover_color "#00aaff"
                textbutton _("Back") action SetScreenVariable("choosing", False) text_idle_color "#777777" text_hover_color "#00aaff"

            text esc("Cards are kept in " + cards_dir()) size 22 color "#999999"
