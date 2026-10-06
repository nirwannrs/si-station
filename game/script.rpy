label start:

    ## Back, Skip and Auto act on Ren'Py dialogue, which this game does not use.
    $ quick_menu = False

    if not llm_ready():
        "No AI model is set up yet. The Models screen opens next: pick a provider, enter your key and a model, then press Return."
        $ renpy.call_in_new_context("_game_menu", _game_menu_screen="models")

    if not llm_ready():
        "A model is needed to play. You can set one up any time from Models in the main menu."
        return

    $ sync_context_size()
    $ start_card(persistent.card, chosen_persona())
    $ start_suggestions()

    show screen thinking
    $ direct_opening()
    hide screen thinking

    ## Cards made without assets skip the stage and the click-through text box.
    if current_card().visual:
        show screen stage
        $ present()

label play:

    ## Waiting for the player from here. A save made at the input screen resumes at this line.
    $ open_turn()

    ## The story of a text-only card is drawn by its own screen, kept up for as long as it is
    ## played. It is shown after the line above, not before it: a loaded save carries on from that
    ## line, and a save made before this screen existed does not have it up.
    if not current_card().visual:
        show screen story_panel

    ## A fight in a card that runs its own fights is played out on the battle screen; the story
    ## picks up again when it ends.
    if game_state["battle"]:
        call screen battle
        if game_state["game_over"]:
            "You were defeated, and the story ends here. Load a save to try again."
            $ renpy.full_restart()
        $ player_text = "(The fight is over.)"
        $ skip_resolve = True
    else:
        call screen turn_input
        $ player_text = _return.strip() if isinstance(_return, str) else ""

    if player_text:
        show screen thinking
        $ play_turn(player_text, resolve=not skip_resolve)
        $ skip_resolve = False
        hide screen thinking
        if turn_error is None:
            $ start_suggestions()
            if current_card().visual:
                $ present(game_state["history"][-1])

    jump play
