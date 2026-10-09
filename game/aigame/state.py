"""The game state: everything that changes during play.

It is built from plain dicts, lists, strings and numbers only, so it pickles into Ren'Py saves and
can be deep-copied for undo. The card itself is never stored in it.
"""

import re
from .card import CAPABILITIES, PLAYER, SENSES


def _new_actor(card, name, start, location):
    start = start or {}
    stats = dict((s["id"], s["default"]) for s in card.data["rules"]["stats"])
    stats.update(start.get("stats", {}))
    own_max = dict((stat, most) for stat, most in start.get("max", {}).items() if stat in stats)
    for stat, most in own_max.items():
        if stat not in start.get("stats", {}):
            stats[stat] = most                      # with no starting value given, they start full
        stats[stat] = min(stats[stat], most)
    own_min = dict((stat, low) for stat, low in start.get("min", {}).items() if stat in stats)
    for stat, low in own_min.items():
        stats[stat] = max(stats[stat], low)
    return {
        "name": name,
        "stats": stats,
        "money": start.get("money", 0),
        "inventory": dict((s["item"], s.get("qty", 1)) for s in start.get("inventory", [])),
        "equipment": dict(start.get("equipment", {})),
        "location": location,
        "level": start.get("level", 1),
        "xp": 0,
        # stat id -> this actor's own maximum: what the card gives them, raised by levels and growth since
        "max": own_max,
        # stat id -> this actor's own minimum, where the card gives them one
        "min": own_min,
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


def _stops(card, state_id, held):
    """What one state stops: the card's word on a state it defines, else what the story said the
    state stops when it made it up."""
    known = card.states.get(state_id)
    return known.get("blocks", []) if known else held.get("stops", [])


def blocked(card, actor, capability):
    """The name of a state that stops the actor doing this, or None."""
    if not card.has("states"):
        return None
    for state_id, held in sorted(actor.get("states", {}).items()):
        blocks = _stops(card, state_id, held)
        if capability in blocks or ("all" in blocks and capability not in SENSES):
            return held["name"]
    return None


def unaware(card, actor):
    """Whether a state has taken the actor out of things altogether: out of the scene, or able
    neither to act nor to see or hear what goes on (asleep, unconscious). Someone like that cannot
    even try. Any other state is a hold on someone who is still there to struggle against it."""
    if not card.has("states"):
        return False
    for state_id, held in actor.get("states", {}).items():
        stops = _stops(card, state_id, held)
        if card.states.get(state_id, {}).get("away") or ("all" in stops and "sight" in stops and "hearing" in stops):
            return True
    return False


def limits(card, actor):
    """Everything the actor's states stop, as (what, name of the state), in a steady order. "all"
    stands for every action, and the single actions it covers are then left out."""
    if not card.has("states"):
        return []
    found = {}
    for state_id, held in sorted(actor.get("states", {}).items()):
        for what in _stops(card, state_id, held):
            found.setdefault(what, held["name"])
    order = ("all",) + (SENSES if "all" in found else CAPABILITIES)
    return [(what, found[what]) for what in order if what in found]


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
    """persona is the player's own (name, description, appearance), used in place of the card's
    default_persona. Whatever it gives is used as given, empty included, so a player's own persona
    is never mixed with the description the card wrote for someone else; only a part it leaves
    out altogether falls back to the card's. What the player starts with is always the card's."""
    default = card.data.get("default_persona", {})
    persona = persona or {}
    player = _new_actor(
        card,
        persona.get("name") or default.get("name") or "Traveler",
        default.get("start"),
        card.data["world"].get("start_location"),
    )
    player["description"] = persona["description"] if "description" in persona else default.get("description", "")
    player["appearance"] = persona["appearance"] if "appearance" in persona else default.get("appearance", "")

    actors = {PLAYER: player}
    for c in card.data.get("characters", []):
        actors[c["id"]] = _new_actor(card, c["name"], c.get("start"), c.get("location"))

    state = {
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
        # places the story created that the card never defined, same shape as card locations, with
        # "temporary" set on those that vanish once everyone has left them
        "generated_locations": {},
        # True while the game waits for the player's next message; False once that message has been played
        "open": True,
        # a copy of this whole state from just before the turn now in progress, or None between turns
        "restore_point": None,
        # ids of the locations on the player's map. Places the card marks hidden join it when the story reveals them.
        "revealed": [l["id"] for l in card.data.get("locations", []) if not l.get("hidden") or l["id"] == card.data["world"].get("start_location")],
        # who speaks in each paragraph of the card's opening, filled in once by the scene director
        "opening_direction": [],
        # character id -> the expression they last wore
        "expressions": {},
    }
    for quest_id, progress in state["quests"].items():
        progress["met"] = quest_marks(card, state, card.quests[quest_id])
    # What happened in each scene so far, and the turn the entries reach up to. See journal.py.
    state["journal"] = []
    state["journal_upto"] = 0
    # character ids in the order they entered the story. See cast_in_play.
    state["cast"] = []
    state["cast"] = cast_in_play(card, state)
    return state


def all_items(card, state):
    items = dict(card.items)
    items.update(state["generated_items"])
    return items


def get_item(card, state, item_id):
    return card.items.get(item_id) or state["generated_items"].get(item_id)


def stat_max(card, actor, stat_id):
    """The most the stat can be for this actor, or None if it has no ceiling."""
    return actor.get("max", {}).get(stat_id, card.stats[stat_id].get("max"))


def stat_min(card, actor, stat_id):
    """The least the stat can be for this actor."""
    return actor.get("min", {}).get(stat_id, card.stats[stat_id].get("min", 0))


def clamp_stat(card, actor, stat_id, value):
    value = max(value, stat_min(card, actor, stat_id))
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
    if "journal" not in state:
        # A save from before the journal: it starts from here, not from scenes the summary has already taken in.
        state["journal"], state["journal_upto"] = [], len(state.get("history", []))
    if "cast" not in state:
        # A save from before the cast was tracked: everyone the story has named so far has entered it.
        told = "\n".join("%s\n%s" % (t["player"], t["narration"]) for t in state.get("history", []))
        state["cast"] = [c for c in fresh["cast"]] + [c for c in named_in(card, told) if c not in fresh["cast"]]
    for key, value in fresh.items():
        state.setdefault(key, value)

    for item in state["generated_items"].values():
        # What a story-made item does to a stat the card no longer has is forgotten; the item stays.
        if "effects" in item:
            item["effects"] = [e for e in item["effects"] if e["stat"] in card.stats]
        if item["type"] == "equipment" and not card.has("equipment"):
            item["type"] = "misc"
            item.pop("slot", None)

    for entry in state.get("journal", []):
        # Entries from before the journal was made to use the player's character's name.
        for part in ("title", "content"):
            entry[part] = re.sub(r"\b(?:[Tt]he player|Player)\b(?! character)", lambda m: state["actors"][PLAYER]["name"], entry.get(part, ""))

    for who in [w for w in state["actors"] if w not in fresh["actors"]]:
        notes.append("%s is no longer in this card." % state["actors"].pop(who)["name"])
    for who, starting in fresh["actors"].items():
        actor = state["actors"].setdefault(who, starting)
        for key, value in starting.items():
            actor.setdefault(key, value)
        for skill_id in [s for s in actor["skills"] if s not in card.skills]:
            del actor["skills"][skill_id]
        actor["max"] = dict((s, v) for s, v in actor["max"].items() if s in card.stats)
        for stat, most in starting["max"].items():
            # A maximum the card has given them since this game began. One the game has already
            # raised or lowered for them stays as it is.
            if stat not in actor["max"]:
                actor["max"][stat] = most
                actor["stats"][stat] = min(actor["stats"].get(stat, most), most)
        actor["min"] = dict(starting["min"])                 # the card's alone; nothing in play changes it
        for stat in actor["min"]:
            actor["stats"][stat] = clamp_stat(card, actor, stat, actor["stats"].get(stat, card.stats[stat]["default"]))
        for item_id in [i for i in actor["inventory"] if get_item(card, state, i) is None]:
            del actor["inventory"][item_id]
            notes.append("%s lost an item this card no longer has (%s)." % (actor["name"], item_id))
        for slot, item_id in list(actor["equipment"].items()):
            item = get_item(card, state, item_id)
            if item is None or item.get("slot") != slot:
                del actor["equipment"][slot]
                notes.append("%s lost equipment this card no longer has (%s)." % (actor["name"], item_id))
        actor["stats"] = dict((s, actor["stats"].get(s, default)) for s, default in starting["stats"].items())
        if actor["location"] not in places(card, state):
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



def places(card, state):
    """Every location in play: the card's, plus any the story has created. id -> location."""
    made = state.get("generated_locations")
    if not made:
        return card.locations
    merged = dict(card.locations)
    merged.update(made)
    return merged


def place_list(card, state):
    return list(card.data.get("locations", [])) + list(state.get("generated_locations", {}).values())


def left_place(state, location_id):
    """Called when someone has just left a place. A temporary place the story made disappears, from
    the map too, once nobody is in it."""
    place = state.get("generated_locations", {}).get(location_id)
    if place and place.get("temporary") and not any(a["location"] == location_id for a in state["actors"].values()):
        del state["generated_locations"][location_id]
        if location_id in state["revealed"]:
            state["revealed"].remove(location_id)


def knows_place(state, location_id):
    """Whether a location is on the player's map."""
    return location_id in state["revealed"]


def reveal(card, state, location_ids):
    """Puts hidden locations on the player's map. Returns the names of the ones that were new to them."""
    new = []
    for location_id in location_ids:
        if location_id in places(card, state) and location_id not in state["revealed"]:
            state["revealed"].append(location_id)
            new.append(places(card, state)[location_id]["name"])
    return new


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
            came_from = actor["location"]
            actor["location"] = here if place == "here" else place if place in places(card, state) else None
            if came_from != actor["location"]:
                left_place(state, came_from)
        if "note" in update:
            actor["note"] = update["note"]
        if update.get("location") == "here":
            actor["known"] = True
        if (actor["location"], actor["note"]) != before:
            where = places(card, state).get(actor["location"])
            changed.append("%s: %s%s" % (actor["name"], where["name"] if where else "off the map", " (%s)" % actor["note"] if actor["note"] else ""))
    return changed

# Objectives the game can check by itself, with no model involved.

CONDITIONS = ("has_item", "at", "level", "relationship")


def condition_met(card, state, condition):
    """One entry of an objective's done_if. Anything it cannot make sense of is simply not met."""
    actor = state["actors"].get(condition.get("who") or PLAYER)
    kind = condition.get("type")
    if kind == "has_item":
        return actor is not None and actor["inventory"].get(condition.get("item"), 0) >= 1
    if kind == "at":
        return actor is not None and actor["location"] is not None and actor["location"] == condition.get("location")
    if kind == "level":
        return state["actors"][PLAYER].get("level", 1) >= (condition.get("at_least") or 0)
    if kind == "relationship":
        return actor is not None and condition.get("who") and actor.get("relationship", 0) >= (condition.get("at_least") or 0)
    return False


NEEDS_FEATURE = {"has_item": "inventory", "level": "levels", "relationship": "relationships"}


def game_checked(card, stage):
    """The objective's conditions, when the game can check them. A card that has switched off a
    system a condition relies on (items, say) leaves the objective to the story instead."""
    conditions = stage.get("done_if") or []
    if any(not isinstance(c, dict) or (c.get("type") in NEEDS_FEATURE and not card.has(NEEDS_FEATURE[c["type"]])) for c in conditions):
        return []
    return conditions


def stage_met(card, state, stage):
    """True when the objective has conditions the game checks and all of them hold now."""
    conditions = game_checked(card, stage)
    return bool(conditions) and all(condition_met(card, state, c) for c in conditions)


def quest_marks(card, state, quest):
    """Ids of the quest's objectives whose game-checked conditions hold right now."""
    return [s["id"] for s in quest["stages"] if stage_met(card, state, s)]


# Who and what the story model needs to hear about. A model uses whatever it is shown, so the
# rule throughout is to show only what is in play: the people who have entered the story, the
# quests that can begin now. Nothing is listed in order to say "not this".

def named_in(card, text):
    """Ids of the characters a text names, in card order. A full name matches in any case; a first
    name must be written as a name ("Mira", not "the marsh" for someone called Marsh Bandit)."""
    found = []
    for character in card.data.get("characters", []):
        name = character["name"].strip()
        first = name.split()[0] if name else ""
        if not name:
            continue
        if re.search(r"(?<!\w)%s(?!\w)" % re.escape(name), text, re.I) or (len(first) >= 3 and re.search(r"(?<!\w)%s(?!\w)" % re.escape(first), text)):
            found.append(character["id"])
    return found


def cast_in_play(card, state, text=""):
    """The characters who have entered the story, in the order they did: those already noted, then
    anyone now with the player, anyone the given text names (what the player just typed, what the
    story just said), and anyone the card's own direction names (its opening and scenario, and
    the objective being played). The list only ever grows at its end, which keeps what was sent
    before identical for the provider's cache."""
    me = state["actors"][PLAYER]
    world = card.data.get("world", {})
    told = [text, world.get("opening", ""), world.get("scenario", ""), world.get("narrator_instructions", "")]
    for quest_id, progress in state.get("quests", {}).items():
        quest = card.quests.get(quest_id)
        if quest and progress["status"] == "active":
            stage = quest["stages"][min(progress["stage"], len(quest["stages"]) - 1)]
            told += [stage.get("description", ""), stage.get("guidance") or stage.get("hint") or "", stage.get("done_when", ""), quest.get("fail_when", "")]
    named = set(named_in(card, "\n".join(told)))
    cast = [c for c in state.get("cast", []) if c in card.characters]
    for character in card.data.get("characters", []):
        who = character["id"]
        actor = state["actors"].get(who)
        here = actor is not None and actor["location"] is not None and actor["location"] == me["location"]
        if who not in cast and (here or who in named):
            cast.append(who)
    return cast


def note_cast(card, state, text=""):
    """Records who has entered the story by now. Call it with each new piece of story."""
    state["cast"] = cast_in_play(card, state, text)


def available_quests(card, state):
    """Quests that have not begun and could begin now. One that follows another waits for it to be
    done. One that names who gives it or where it starts is only on offer with that character
    present or at that place; one that names neither can start anywhere."""
    me = state["actors"][PLAYER]
    found = []
    for quest in card.data.get("quests", []):
        if quest["id"] in state["quests"]:
            continue
        if quest.get("after") and state["quests"].get(quest["after"], {}).get("status") != "done":
            continue
        giver, place = state["actors"].get(quest.get("giver")), quest.get("start_location")
        if quest.get("giver") or place:
            with_giver = giver is not None and giver["location"] is not None and giver["location"] == me["location"] and not is_away(card, giver)
            if not (with_giver or (place and place == me["location"])):
                continue
        found.append(quest)
    return found
