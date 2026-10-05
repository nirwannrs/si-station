## Glue between Ren'Py and the card engine in game/aigame.

## Ren'Py rollback cannot rewind LLM calls, so it is off; undo will work from state snapshots.
define config.rollback_enabled = False

## Folder name of the card in play, inside cards/. Saves keep this and game_state, never the card.
default card_name = None
default game_state = None
## Results of recent actions, newest last.
default game_log = []
## What happened in the latest round of a fight.
default battle_log = []

init python:
    import os
    from aigame import actions as aig_actions
    from aigame import battle as aig_battle
    from aigame import card as aig_card
    from aigame import state as aig_state

    def cards_dir():
        return os.path.join(config.basedir, "cards")

    def current_card():
        return aig_card.get_card(os.path.join(cards_dir(), store.card_name))

    def start_card(name, persona=None):
        store.card_name = name
        store.game_state = aig_state.new_game(current_card(), persona)
        store.game_log = []
        store.battle_log = []

    from aigame import text as aig_text

    ## Makes card or LLM text safe to show exactly as it is.
    esc = aig_text.escape
    ## The same, with *italic*, **bold** and the like applied. For story text and what the player typed.
    rich = aig_text.to_renpy

    def card_text(text):
        return esc(text.replace("{{user}}", store.game_state["actors"][aig_card.PLAYER]["name"]))

    def do_action(**action):
        """Applies one action from a button. Returns nothing on purpose: a value returned by a
        button's function would end the screen the button is on."""
        result = aig_actions.apply_action(current_card(), store.game_state, action, by_player=True)
        store.game_log = store.game_log[-29:] + [result]
        # The narrator hears about button actions at the start of the next turn.
        store.game_state["pending_results"].append(result)
        renpy.restart_interaction()

    def do_battle(**action):
        """Plays one round of the fight. The narrator hears the whole fight when it is over."""
        results = aig_battle.take_turn(current_card(), store.game_state, action)
        store.battle_log = results
        store.game_state["pending_results"].extend(results)
        renpy.restart_interaction()

    def go_to(location_id):
        """The map's Go button. Travelling is part of the story, so it is played as a turn at once
        and the narrator gets to say what leaving means, instead of the scene carrying on regardless."""
        do_action(type="move", location=location_id)
        if not store.game_state["pending_results"][-1]["ok"]:
            return None
        renpy.hide_screen("hud")
        store.skip_resolve = True
        return "I go to %s." % current_card().locations[location_id]["name"]

    def shops_here():
        me = store.game_state["actors"][aig_card.PLAYER]
        return [s for s in current_card().data.get("shops", []) if not s.get("location") or s["location"] == me["location"]]

    def hud_tabs():
        """(name, title) of each overlay tab: only the systems the card uses, and Shop only where there is one."""
        card = current_card()
        tabs = [
            ("inventory", _("Inventory"), card.has("inventory")),
            ("equipment", _("Equipment"), card.has("equipment")),
            ("skills", _("Skills"), card.has("skills")),
            ("people", _("People"), card.has("relationships")),
            ("map", _("Map"), bool(card.locations)),
            ("quests", _("Quests"), bool(card.quests)),
            ("shop", _("Shop"), aig_prompt.uses(card, "shops") and bool(shops_here())),
        ]
        return [(name, title) for name, title, shown in tabs if shown]

    def level_line():
        me = store.game_state["actors"][aig_card.PLAYER]
        return "Level %d   (%g / %g experience)" % (me["level"], me["xp"], aig_state.xp_needed(current_card(), me))

    def skill_cost(skill):
        card = current_card()
        return ", ".join("%s %g" % (card.stats[stat]["name"], amount) for stat, amount in sorted(skill.get("cost", {}).items()))

    def exit_ids(location_id=None):
        """Ids of the locations reachable in one step from location_id (default: where the player is)."""
        card = current_card()
        here = card.locations.get(location_id or store.game_state["actors"][aig_card.PLAYER]["location"])
        if not here:
            return []
        return [l["id"] for l in card.data.get("locations", [])
                if l["id"] != here["id"] and (l["id"] in here.get("connections", []) or here["id"] in l.get("connections", []))]

    def status_line():
        card = current_card()
        me = store.game_state["actors"][aig_card.PLAYER]
        parts = ["Level %d" % me["level"]] if card.has("levels") else []
        if card.has("states") and me["states"]:
            parts.append(aig_state.describe_states(me))
        parts += [stat_line(s) for s in card.data["rules"]["stats"] if "max" in s]
        if card.has("money"):
            parts.append("%s: %g" % (card.currency, me["money"]))
        here = card.locations.get(me["location"])
        return "   ".join(parts + ([here["name"]] if here else []))

    def stat_line(stat):
        me = store.game_state["actors"][aig_card.PLAYER]
        value = aig_state.effective_stat(current_card(), store.game_state, aig_card.PLAYER, stat["id"])
        ceiling = aig_state.stat_max(current_card(), me, stat["id"])
        if ceiling is not None:
            return "%s: %g / %g" % (stat["name"], value, ceiling)
        return "%s: %g" % (stat["name"], value)


## Overlay with one tab per thing the player checks often. Opened on a given tab from the play screen.
screen hud(start="inventory"):
    modal True
    zorder 10
    default tab = start
    $ people_here = [(w, a) for w, a in sorted(game_state["actors"].items()) if w != "player" and a["location"] == game_state["actors"]["player"]["location"] and not aig_state.is_away(current_card(), a)]
    $ card = current_card()
    $ me = game_state["actors"]["player"]
    $ here = card.locations.get(me["location"])
    $ shops = shops_here()

    add "#000000c0"
    key "game_menu" action Hide("hud")

    frame:
        align (0.5, 0.5)
        xsize 1640
        ysize 960
        padding (40, 30)

        vbox:
            spacing 20

            hbox:
                spacing 40
                for name, title in hud_tabs():
                    textbutton title action SetScreenVariable("tab", name) selected (tab == name) text_size 36

            hbox:
                spacing 40

                vbox:
                    xsize 380
                    spacing 6
                    text esc(me["name"]) size 40
                    if card.has("levels"):
                        text esc(level_line()) size 26
                    if card.has("states") and me["states"]:
                        text esc(aig_state.describe_states(me)) size 26 color "#ffb070"
                    for stat in card.data["rules"]["stats"]:
                        text esc(stat_line(stat))
                    if card.has("money"):
                        text esc("%s: %g" % (card.currency, me["money"]))
                    if here:
                        null height 10
                        text esc("At: " + here["name"]) color "#aaaaaa"

                viewport:
                    xsize 1120
                    ysize 640
                    scrollbars "vertical"
                    mousewheel True
                    draggable True

                    vbox:
                        spacing 10

                        if tab == "inventory":
                            for item_id, qty in sorted(me["inventory"].items()):
                                $ item = aig_state.get_item(card, game_state, item_id)
                                hbox:
                                    spacing 16
                                    text esc("%s x%d" % (item["name"], qty)) yalign 0.5
                                    if item["type"] == "consumable":
                                        textbutton _("Use") action Function(do_action, type="use_item", item=item_id)
                                    elif item["type"] == "equipment":
                                        textbutton _("Equip") action Function(do_action, type="equip", item=item_id)
                                if item.get("description"):
                                    text esc(item["description"]) size 24 color "#aaaaaa"
                            if not me["inventory"]:
                                text _("You are carrying nothing.")

                        elif tab == "equipment":
                            for slot in aig_card.SLOTS:
                                hbox:
                                    spacing 16
                                    if slot in me["equipment"]:
                                        text esc("%s: %s" % (slot.capitalize(), aig_state.get_item(card, game_state, me["equipment"][slot])["name"])) yalign 0.5
                                        textbutton _("Take off") action Function(do_action, type="unequip", slot=slot)
                                    else:
                                        text esc("%s: empty" % slot.capitalize()) color "#888888"
                            null height 10
                            text _("Can be equipped") size 34
                            for item_id, qty in sorted(me["inventory"].items()):
                                $ item = aig_state.get_item(card, game_state, item_id)
                                if item["type"] == "equipment":
                                    hbox:
                                        spacing 16
                                        text esc("%s (%s)" % (item["name"], item["slot"])) yalign 0.5
                                        textbutton _("Equip") action Function(do_action, type="equip", item=item_id)

                        elif tab == "skills":
                            for skill_id, known in sorted(me["skills"].items()):
                                $ skill = card.skills[skill_id]
                                $ cost = skill_cost(skill)
                                if known["unlocked"]:
                                    hbox:
                                        spacing 16
                                        text esc(skill["name"] + ("  (%s)" % cost if cost else "")) yalign 0.5
                                        if skill.get("target", "other") == "self":
                                            textbutton _("Use") action Function(do_action, type="use_skill", skill=skill_id)
                                        else:
                                            for who, actor in people_here:
                                                textbutton esc("Use on " + actor["name"]) action Function(do_action, type="use_skill", skill=skill_id, target=who)
                                else:
                                    text esc("%s  (locked%s)" % (skill["name"], ", unlocks at level %d" % known["unlock_level"] if known.get("unlock_level") else "")) color "#888888"
                                if skill.get("description"):
                                    text esc(skill["description"]) size 24 color "#aaaaaa"
                            if not me["skills"]:
                                text _("You have no skills yet.")

                        elif tab == "people":
                            ## Only people the player has met. Strangers appear here once the story introduces them.
                            for c in [c for c in card.data.get("characters", []) if game_state["actors"][c["id"]].get("known", True)]:
                                $ actor = game_state["actors"][c["id"]]
                                hbox:
                                    spacing 20
                                    text esc(c["name"]) color speaker_color(c["id"]) min_width 360 yalign 0.5
                                    bar value StaticValue(actor["relationship"], 100) xsize 360 yalign 0.5
                                    text esc("%s %g / 100" % (card.relationship_name, actor["relationship"])) size 26 yalign 0.5
                                if card.has("states") and actor["states"]:
                                    text esc(aig_state.describe_states(actor)) size 24 color "#ffb070"
                                text esc(("Here with you. " if actor["location"] == me["location"] and not aig_state.is_away(card, actor) else "") + c["description"]) size 24 color "#aaaaaa"
                            if not [c for c in card.data.get("characters", []) if game_state["actors"][c["id"]].get("known", True)]:
                                text _("You have not met anyone yet.")

                        elif tab == "map":
                            if game_state["travel_lock"] is not None:
                                text esc("You cannot leave right now: " + game_state["travel_lock"]) color "#ffb070"
                                null height 6
                            for place in card.data.get("locations", []):
                                hbox:
                                    spacing 16
                                    if here and place["id"] == here["id"]:
                                        text esc(place["name"] + " (you are here)") yalign 0.5
                                    elif place["id"] in exit_ids():
                                        text esc(place["name"]) yalign 0.5
                                        if game_state["travel_lock"] is None:
                                            textbutton _("Go") action Function(go_to, place["id"])
                                    else:
                                        text esc(place["name"]) color "#888888" yalign 0.5
                                $ ways = [card.locations[i]["name"] for i in exit_ids(place["id"])]
                                if ways:
                                    text esc("Leads to: " + ", ".join(ways)) size 24 color "#aaaaaa"
                                if here and place["id"] == here["id"]:
                                    $ people = [a["name"] if a.get("known", True) else "someone you do not know" for w, a in sorted(game_state["actors"].items()) if w != "player" and a["location"] == here["id"] and not aig_state.is_away(card, a)]
                                    if people:
                                        text esc("Here: " + ", ".join(people)) size 24 color "#aaaaaa"
                            if not card.data.get("locations"):
                                text _("This story has no map.")

                        elif tab == "quests":
                            for quest in card.data.get("quests", []):
                                $ progress = game_state["quests"].get(quest["id"])
                                if progress and progress["status"] == "active":
                                    text esc(quest["title"]) size 34
                                    text esc(quest["stages"][progress["stage"]]["description"])
                                elif progress:
                                    text esc("%s (%s)" % (quest["title"], progress["status"])) color "#888888"
                            if not game_state["quests"]:
                                text _("No quests yet.")

                        elif tab == "shop":
                            for shop in shops:
                                text esc(shop["name"]) size 34
                                for item_id, left in sorted(game_state["shops"][shop["id"]].items()):
                                    $ item = aig_state.get_item(card, game_state, item_id)
                                    hbox:
                                        spacing 16
                                        text esc("%s, %g %s%s" % (item["name"], aig_actions.shop_price(card, game_state, shop["id"], item_id), card.currency, "" if left is None else " (%d left)" % left)) yalign 0.5
                                        textbutton _("Buy") action Function(do_action, type="buy", shop=shop["id"], item=item_id)
                                for item_id in sorted(me["inventory"]):
                                    $ pay = aig_actions.sell_price(card, game_state, shop["id"], item_id)
                                    if pay > 0:
                                        hbox:
                                            spacing 16
                                            text esc("Your %s, for %g %s" % (aig_state.get_item(card, game_state, item_id)["name"], pay, card.currency)) yalign 0.5
                                            textbutton _("Sell") action Function(do_action, type="sell", shop=shop["id"], item=item_id)
                            if not shops:
                                text _("There is no shop here.")

            if game_state["pending_results"]:
                $ result = game_state["pending_results"][-1]
                text esc(result["message"]) size 24 color ("#9fd89f" if result["ok"] else "#ff8080")

        textbutton _("Close") action Hide("hud") xalign 1.0 yalign 1.0


## A fight the game runs itself: one choice per round, then the enemies act. Stays up after the
## last round so the player can read how it ended.
screen battle():
    $ card = current_card()
    $ me = game_state["actors"]["player"]
    $ fight = game_state["battle"]
    $ health = card.battle["health_stat"]
    $ foes = aig_battle.standing(card, game_state) if fight else []

    frame:
        xfill True
        yalign 1.0
        padding (60, 24)
        background "#000000e0"

        vbox:
            spacing 16

            if fight:
                text esc("Fight, round %d" % fight["round"]) size 40

                hbox:
                    spacing 60

                    vbox:
                        xsize 380
                        spacing 4
                        text esc(me["name"]) size 34
                        if card.has("states") and me["states"]:
                            text esc(aig_state.describe_states(me)) size 24 color "#ffb070"
                        for stat in card.data["rules"]["stats"]:
                            if aig_state.stat_max(card, me, stat["id"]) is not None:
                                text esc(stat_line(stat)) size 28

                    vbox:
                        xsize 640
                        spacing 8
                        for enemy in fight["enemies"]:
                            $ actor = game_state["actors"][enemy]
                            $ most = aig_state.stat_max(card, actor, health) or max(actor["stats"][health], 1)
                            hbox:
                                spacing 16
                                text esc(actor["name"] + (" (%s)" % aig_state.describe_states(actor) if card.has("states") and actor["states"] else "")) min_width 240 yalign 0.5 color (speaker_color(enemy) if enemy in card.characters else "#ffffff")
                                bar value StaticValue(max(actor["stats"][health], 0), most) xsize 200 yalign 0.5
                                if enemy in foes:
                                    textbutton _("Attack") action Function(do_battle, type="battle_attack", target=enemy)
                                else:
                                    text _("down") color "#888888" yalign 0.5

                    vbox:
                        spacing 4
                        if card.has("skills"):
                            for skill_id, known in sorted(me["skills"].items()):
                                $ skill = card.skills[skill_id]
                                $ label = skill["name"] + (" (%s)" % skill_cost(skill) if skill_cost(skill) else "")
                                if known["unlocked"] and skill.get("target", "other") == "self":
                                    textbutton esc(label) action Function(do_battle, type="use_skill", skill=skill_id) text_size 28
                                elif known["unlocked"]:
                                    for enemy in foes:
                                        textbutton esc("%s on %s" % (label, game_state["actors"][enemy]["name"])) action Function(do_battle, type="use_skill", skill=skill_id, target=enemy) text_size 28
                        if card.has("inventory"):
                            for item_id, qty in sorted(me["inventory"].items()):
                                $ item = aig_state.get_item(card, game_state, item_id)
                                if item["type"] == "consumable":
                                    textbutton esc("Use %s x%d" % (item["name"], qty)) action Function(do_battle, type="use_item", item=item_id) text_size 28
                        textbutton _("Wait") action Function(do_battle, type="battle_wait") text_size 28
                        textbutton _("Run") action Function(do_battle, type="battle_flee") text_size 28

            else:
                text _("The fight is over") size 40

            vbox:
                for result in battle_log:
                    text esc(result["message"]) size 24 color ("#9fd89f" if result["ok"] else "#ff8080")

            if not fight:
                textbutton _("Continue") action Return()
