"""The game state: everything that changes during play.

It is built from plain dicts, lists, strings and numbers only, so it pickles into Ren'Py saves and
can be deep-copied for undo. The card itself is never stored in it.
"""

from .card import PLAYER


def _new_actor(card, name, start, location):
    start = start or {}
    stats = dict((s["id"], s["default"]) for s in card.data["rules"]["stats"])
    stats.update(start.get("stats", {}))
    return {
        "name": name,
        "stats": stats,
        "money": start.get("money", 0),
        "inventory": dict((s["item"], s.get("qty", 1)) for s in start.get("inventory", [])),
        "equipment": dict(start.get("equipment", {})),
        "location": location,
        "level": start.get("level", 1),
        "xp": 0,
        # stat id -> this actor's own maximum once levelling has raised it above the card's
        "max": {},
        # skill id -> {"unlocked", "unlock_level"}; a locked skill is known about but cannot be used yet
        "skills": dict((s["skill"], {"unlocked": not s.get("locked", False), "unlock_level": s.get("unlock_level")})
                       for s in start.get("skills", [])),
        # how this character feels about the player, 0 to 100; unused on the player
        "relationship": start.get("relationship", 0),
        # whether the player knows who this is. Characters the card starts as strangers stay out of the
        # player's lists until the story has them speak or join the player.
        "known": start.get("known", True),
        # what they are up to or where they went, in a few words, as last worked out from the story; "" if nothing is known
        "note": "",
        # state id -> {"name", "note"}: conditions the actor is in right now, such as restrained or asleep
        "states": dict((s["state"], {"name": state_name(card, s["state"]), "note": s.get("note", "")}) for s in start.get("states", [])),
    }


def state_name(card, state_id):
    known = card.states.get(state_id)
    return known["name"] if known else state_id.replace("_", " ").capitalize()


def blocked(card, actor, capability):
    """The name of a state that stops the actor doing this, or None. States the card does not define block nothing."""
    if not card.has("states"):
        return None
    for state_id, held in sorted(actor.get("states", {}).items()):
        blocks = card.states.get(state_id, {}).get("blocks", [])
        if "all" in blocks or capability in blocks:
            return held["name"]
    return None


def is_away(card, actor):
    """True while a state has taken the actor out of the scene."""
    return card.has("states") and any(card.states.get(s, {}).get("away") for s in actor.get("states", {}))


def together(card, a, b):
    """Whether two actors can reach each other: same place (when both have one) and neither away."""
    if is_away(card, a) or is_away(card, b):
        return False
    return not (a["location"] and b["location"] and a["location"] != b["location"])


def describe_states(actor):
    return ", ".join(held["name"] + (" (%s)" % held["note"] if held.get("note") else "") for state_id, held in sorted(actor.get("states", {}).items()))


def new_game(card, persona=None):
    """persona is what the player typed in game (name, description, appearance); it overrides the card's default_persona text."""
    default = card.data.get("default_persona", {})
    persona = persona or {}
    player = _new_actor(
        card,
        persona.get("name") or default.get("name") or "Traveler",
        default.get("start"),
        card.data["world"].get("start_location"),
    )
    player["description"] = persona.get("description") or default.get("description", "")
    player["appearance"] = persona.get("appearance") or default.get("appearance", "")

    actors = {PLAYER: player}
    for c in card.data.get("characters", []):
        actors[c["id"]] = _new_actor(card, c["name"], c.get("start"), c.get("location"))

    return {
        "card_id": card.id,
        "turn": 0,
        "actors": actors,
        # quest id -> {"status": "active" | "done" | "failed", "stage": index into the quest's stages}
        "quests": dict((q["id"], {"status": "active", "stage": 0}) for q in card.data.get("quests", []) if q.get("auto_start")),
        # shop id -> {item id -> qty left, or None for unlimited}
        "shops": dict((s["id"], dict((e["item"], e.get("qty")) for e in s.get("stock", []))) for s in card.data.get("shops", [])),
        # items the narrator invented during play, same shape as card items
        "generated_items": {},
        # finished turns, oldest first: {"player": text, "results": [{"ok", "message"}], "narration": text}
        "history": [],
        # what happened in the first `summarized` turns; those are kept for the player's log but no longer sent to the model
        "summary": "",
        "summarized": 0,
        # results of actions done through UI buttons since the last turn; the narrator is told about them next turn
        "pending_results": [],
        # the fight in progress, when the card runs fights itself: {"enemies": [character ids], "round": n}
        "battle": None,
        # set when a defeat ends the story
        "game_over": False,
        # why the player may not travel right now, when the narrator has closed the map to them; else None
        "travel_lock": None,
        # who speaks in each paragraph of the card's opening, filled in once by the scene director
        "opening_direction": [],
        # character id -> the expression they last wore
        "expressions": {},
    }


def all_items(card, state):
    items = dict(card.items)
    items.update(state["generated_items"])
    return items


def get_item(card, state, item_id):
    return card.items.get(item_id) or state["generated_items"].get(item_id)


def stat_max(card, actor, stat_id):
    """The most the stat can be for this actor, or None if it has no ceiling."""
    return actor.get("max", {}).get(stat_id, card.stats[stat_id].get("max"))


def clamp_stat(card, actor, stat_id, value):
    value = max(value, card.stats[stat_id].get("min", 0))
    ceiling = stat_max(card, actor, stat_id)
    return value if ceiling is None else min(value, ceiling)


def xp_needed(card, actor):
    """Experience needed to finish the actor's current level. Each level takes a little more."""
    return card.xp_per_level * actor["level"]


def effective_stat(card, state, who, stat_id):
    """Base value plus bonuses from equipped items. Equipment never changes the stored base value."""
    actor = state["actors"][who]
    value = actor["stats"].get(stat_id, card.stats[stat_id]["default"])
    for item_id in actor["equipment"].values():
        item = get_item(card, state, item_id)
        for effect in (item or {}).get("effects", []):
            if effect["stat"] == stat_id:
                value += effect["amount"]
    return clamp_stat(card, actor, stat_id, value)


def reconcile(card, state):
    """Makes a loaded save fit the card as it is now, since a card can be replaced by a newer copy.

    Anything the card no longer defines is dropped and anything new is added with its starting
    values. Returns a list of sentences describing what was dropped, for the player.
    """
    fresh = new_game(card)
    notes = []
    for key, value in fresh.items():
        state.setdefault(key, value)

    for who in [w for w in state["actors"] if w not in fresh["actors"]]:
        notes.append("%s is no longer in this card." % state["actors"].pop(who)["name"])
    for who, starting in fresh["actors"].items():
        actor = state["actors"].setdefault(who, starting)
        for key, value in starting.items():
            actor.setdefault(key, value)
        for skill_id in [s for s in actor["skills"] if s not in card.skills]:
            del actor["skills"][skill_id]
        actor["max"] = dict((s, v) for s, v in actor["max"].items() if s in card.stats)
        for item_id in [i for i in actor["inventory"] if get_item(card, state, i) is None]:
            del actor["inventory"][item_id]
            notes.append("%s lost an item this card no longer has (%s)." % (actor["name"], item_id))
        for slot, item_id in list(actor["equipment"].items()):
            item = get_item(card, state, item_id)
            if item is None or item.get("slot") != slot:
                del actor["equipment"][slot]
                notes.append("%s lost equipment this card no longer has (%s)." % (actor["name"], item_id))
        actor["stats"] = dict((s, actor["stats"].get(s, default)) for s, default in starting["stats"].items())
        if actor["location"] not in card.locations:
            actor["location"] = starting["location"]

    for quest_id in [q for q in state["quests"] if q not in card.quests]:
        del state["quests"][quest_id]
        notes.append("A quest this card no longer has was removed (%s)." % quest_id)
    for quest_id, progress in state["quests"].items():
        progress["stage"] = min(progress["stage"], len(card.quests[quest_id]["stages"]) - 1)

    if state["battle"] and (not card.battle_system or any(e not in state["actors"] for e in state["battle"]["enemies"])):
        state["battle"] = None

    shops = {}
    for shop_id, stock in fresh["shops"].items():
        old = state["shops"].get(shop_id, {})
        shops[shop_id] = dict((item_id, old.get(item_id, qty)) for item_id, qty in stock.items())
    state["shops"] = shops
    return notes



def meet(state, who):
    """Marks characters as known to the player. Unknown ids are ignored."""
    for actor_id in who:
        if actor_id in state["actors"]:
            state["actors"][actor_id]["known"] = True


def track(card, state, updates):
    """Applies what the scene director worked out about where characters are, and returns what changed.

    The narrator often moves people only in prose: someone "heads upstairs" or "walks in" without
    any action to say so. This keeps each character's place in step with the story. It is not held
    to the map's connections, since characters travel off screen, and it never moves the player.
    """
    here = state["actors"][PLAYER]["location"]
    changed = []
    for update in updates:
        actor = state["actors"].get(update.get("id"))
        if actor is None or update["id"] == PLAYER:
            continue
        before = (actor["location"], actor.get("note", ""))
        if "location" in update:
            place = update["location"]
            actor["location"] = here if place == "here" else place if place in card.locations else None
        if "note" in update:
            actor["note"] = update["note"]
        if update.get("location") == "here":
            actor["known"] = True
        if (actor["location"], actor["note"]) != before:
            where = card.locations.get(actor["location"])
            changed.append("%s: %s%s" % (actor["name"], where["name"] if where else "off the map", " (%s)" % actor["note"] if actor["note"] else ""))
    return changed