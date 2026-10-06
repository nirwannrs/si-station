## Who the player is. A card comes with a default persona; the player can write their own, which
## is then used for every card in its place, and can still choose the card's for any one card.
## A persona is a name, a description and an appearance. What the player's character starts the
## game with (stats, items, skills) always comes from the card.

default persistent.persona = {"name": "", "description": "", "appearance": ""}
## card id -> True for cards the player chose to play as the card's own persona.
default persistent.persona_default = {}

init python:

    def has_own_persona():
        return bool((persistent.persona.get("name") or "").strip())

    def uses_default_persona():
        """True when a new game of the inserted card would use the card's persona, not the player's."""
        return not has_own_persona() or bool(persistent.persona_default.get(persistent.card))

    def chosen_persona():
        """The player's own persona for a new game, or None to use the card's."""
        if uses_default_persona():
            return None
        return dict((key, (persistent.persona.get(key) or "").strip()) for key in ("name", "description", "appearance"))

    def persona_in_use(card):
        """The name, description and appearance a new game of this card would be played with."""
        default = card.data.get("default_persona", {}) if card else {}
        return chosen_persona() or {"name": default.get("name") or "Traveler", "description": default.get("description", ""), "appearance": default.get("appearance", "")}

    def choose_persona(default):
        persistent.persona_default[persistent.card] = default
        renpy.restart_interaction()

    def persona_differs():
        """Whether the game in progress is being played as someone other than the persona chosen now."""
        if store.game_state is None or not store.card_name:
            return False
        me, wanted = store.game_state["actors"]["player"], persona_in_use(current_card())
        return any((me.get(key) or "") != wanted[key] for key in ("name", "description", "appearance"))

    def apply_persona():
        """Makes the game in progress use the persona chosen now. The story already told keeps the old name."""
        me = store.game_state["actors"]["player"]
        me.update(persona_in_use(current_card()))
        renpy.restart_interaction()


screen persona_box(title, key, height):
    vbox:
        spacing 6
        text title
        button:
            xsize 1180
            ysize height
            padding (16, 12)
            background "#00000080"
            action settings_field(persistent.persona, key).Toggle()
            viewport:
                scrollbars "vertical"
                mousewheel True
                input value settings_field(persistent.persona, key) multiline True copypaste True xmaximum 1100


screen persona():
    tag menu

    use game_menu(_("Persona"), scroll="viewport"):
        $ card = inserted_card()
        $ default = card.data.get("default_persona", {}) if card else {}
        $ playing = store.game_state is not None and bool(store.card_name) and not main_menu

        vbox:
            spacing 22
            text _("Who you are in the story. Write your own and it is used for every card, in place of the one a card comes with. What you start a game with (stats, items, skills) is always the card's.") size 24

            vbox:
                spacing 10
                label _("Your own persona")
                use settings_input(_("Name"), persistent.persona, "name")
                if not has_own_persona():
                    text _("Give it a name to use it. Until then, each card's own persona is used.") size 22 color "#999999"
                use persona_box(_("Who you are (background, personality, anything the story should know)"), "description", 200)
                use persona_box(_("What you look like"), "appearance", 130)

            if card:
                vbox:
                    spacing 8
                    label esc("For %s" % card.data["meta"]["title"])
                    vbox:
                        style_prefix "radio"
                        xsize 1180
                        textbutton esc("Use my own persona" + (": " + persistent.persona["name"].strip() if has_own_persona() else "")) action Function(choose_persona, False) selected (not uses_default_persona()) sensitive has_own_persona()
                        textbutton esc("Use this card's default: %s" % (default.get("name") or "Traveler")) action Function(choose_persona, True) selected uses_default_persona()
                    if default.get("description") or default.get("appearance"):
                        text esc("The card's default. " + " ".join(part for part in (default.get("description", ""), default.get("appearance", "")) if part)) size 22 color "#999999"

                if playing:
                    vbox:
                        spacing 8
                        label _("The game in progress")
                        text esc("You are playing as %s." % store.game_state["actors"]["player"]["name"]) size 24
                        if persona_differs():
                            text esc("That is not the persona chosen above (%s). A new game will use the one above. You can also switch this game over; the story already told keeps the old name, so expect the next reply or two to settle in." % persona_in_use(card)["name"]) size 22 color "#ffb070"
                            textbutton _("Use it in this game too") action Confirm(_("Change who you are in the game in progress?"), Function(apply_persona))
            else:
                text _("Insert a card to choose between your own persona and the card's.") size 22 color "#999999"
