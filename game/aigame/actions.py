"""Applies actions (see spec/actions.schema.json) to the game state.

Actions come from the LLM or from UI buttons and are never trusted: each one is checked against the
card and the current state, and either applied in full or rejected with a reason. The messages are
plain factual sentences because they are fed back to the narrator.
"""

import re

from .card import PLAYER, SLOTS
from .state import game_checked, knows_place, left_place, places, quest_marks, reveal
from .state import all_items, blocked, clamp_stat, effective_stat, stat_max, stat_min, state_name, together, xp_needed

# The card system each action belongs to. An action for a system the card switched off is rejected.
REQUIRES = {
    "use_item": "inventory", "transfer_item": "inventory", "add_item": "inventory", "remove_item": "inventory",
    "create_item": "inventory", "change_item": "inventory", "equip": "equipment", "unequip": "equipment",
    "buy": "money", "sell": "money", "change_money": "money",
    "gain_xp": "levels", "use_skill": "skills", "unlock_skill": "skills",
    "change_relationship": "relationships",
    "set_state": "states", "clear_state": "states",
}

# What the player must be free to do for each thing they can attempt. A state that blocks it stops
# the attempt. The narrator's own actions are not held to this: guards can drag a bound prisoner off.
NEEDS = {
    "move": "move", "use_item": "items", "transfer_item": "items", "equip": "equipment", "unequip": "equipment",
    "use_skill": "skills", "buy": "trade", "sell": "trade", "start_battle": "attack",
}


class Rejected(Exception):
    pass


def apply_actions(card, state, actions, by_player=False):
    """Applies a list of actions from one source in one turn.

    A quest moves on by at most one objective per list. A model that decides a quest is finished
    tends to say so once for every objective left; only the first is real.
    """
    results, moved_on = [], set()
    ## New places and new items first, whatever order they were listed in: a model often writes
    ## "move there" above "create it", and the move must not fail for want of the place.
    made = ("create_location", "create_item")
    actions = [a for a in actions if isinstance(a, dict) and a.get("type") in made] + [a for a in actions if not (isinstance(a, dict) and a.get("type") in made)]
    for action in actions:
        if isinstance(action, dict) and action.get("type") == "quest_advance":
            quest = str(action.get("quest")).strip().lower()
            if quest in moved_on:
                continue
            result = apply_action(card, state, action, by_player)
            if result["ok"]:
                moved_on.add(quest)
            results.append(result)
        elif isinstance(action, dict) and action.get("type") == "change_stat" and action.get("amount") == 0:
            continue                                # "Mana +0": a model reporting that nothing changed
        else:
            results.append(apply_action(card, state, action, by_player))
    return results + settle_quests(card, state)


def apply_action(card, state, action, by_player=False):
    """Returns {"action", "ok", "message"}. The state is only changed when ok is True.

    by_player marks something the player chose to do, by typing or by a button, as opposed to
    something the narrator says happened. Only the player's own choices are stopped by their states.
    """
    handler = _HANDLERS.get(action["type"]) if isinstance(action, dict) and isinstance(action.get("type"), str) else None
    if handler is None:
        return {"action": action, "ok": False, "message": "Unknown action."}
    if state.get("battle") and action["type"] in ("move", "buy", "sell", "start_battle"):
        return {"action": action, "ok": False, "message": "Not in the middle of a fight."}
    if by_player and action["type"] in NEEDS:
        stopped_by = blocked(card, state["actors"][PLAYER], NEEDS[action["type"]])
        if stopped_by:
            return {"action": action, "ok": False, "message": "%s cannot do that while %s." % (state["actors"][PLAYER]["name"], stopped_by.lower())}
    if by_player and action["type"] == "move":
        try:
            wanted = _find(places(card, state), action.get("location"), "location")[0]
        except Rejected:
            wanted = None
        # The player is never simply stopped from going somewhere real. What the map says is what they
        # can do by walking: a place they are held in, a place that is not on their map and a place
        # that is far off are all beyond that, but a teleporter, someone who knows more than they
        # should, or someone with a guide may get there anyway. So these are neither applied nor
        # refused: ok is None, which hands the attempt to the story model (the player is not shown
        # it), and the story decides whether it works and what it costs them.
        who = state["actors"][PLAYER]["name"]
        if wanted is not None and state.get("travel_lock") is not None:
            return {"action": action, "ok": None, "message": "%s is held where they are (%s) and tries to go to %s all the same. Whether they manage it, by what means, and what comes of it is for the story to decide." % (
                who, state["travel_lock"].rstrip("."), places(card, state)[wanted]["name"])}
        if wanted is None and state.get("travel_lock") is not None:
            return {"action": action, "ok": False, "message": "%s cannot leave right now: %s" % (who, state["travel_lock"])}
        if wanted is not None and not knows_place(state, wanted):
            return {"action": action, "ok": None, "message": "%s means to go to %s, which is not on their map: they have not been shown it or the way there. Whether they get there, by what means, and what comes of it is for the story to decide." % (
                who, places(card, state)[wanted]["name"])}
        player = state["actors"][PLAYER]
        here = places(card, state).get(player["location"])
        if wanted is not None and here and wanted != here["id"] and not _connected(here, places(card, state)[wanted]):
            # The map only says where the player can walk by themselves. Somewhere further off may
            # still be reachable by a portal, a carriage or a week on the road, and that is the
            # story's call, not the engine's. So this is neither applied nor refused: ok is None.
            return {"action": action, "ok": None, "message": "%s wants to go to %s, which is not next to %s. Whether and how they get there is for the story to decide." % (
                player["name"], places(card, state)[wanted]["name"], here["name"])}
    if by_player and action["type"] == "equip" and card.has("equipment") and action.get("slot") not in SLOTS:
        try:
            iid, item = _item(card, state, action)
        except Rejected:
            item = None
        if item and item.get("generated") and item["type"] == "misc" and state["actors"][PLAYER]["inventory"].get(iid):
            # A plain thing the story made, and nothing says where it would be worn. Not refused:
            # the story is told, and can make it wearable.
            return {"action": action, "ok": None, "message": "%s tries to put on or take up %s, which the game holds as a plain item, so it is not yet equipped. If it is something to wear or hold, make it so with change_item and its slot; %s can then equip it." % (
                state["actors"][PLAYER]["name"], item["name"], state["actors"][PLAYER]["name"])}
    if not by_player:
        counted = _paid_already(card, state, action)
        if counted:
            return {"action": action, "ok": True, "message": counted}
    feature = REQUIRES.get(action["type"])
    if feature and not card.has(feature):
        return {"action": action, "ok": False, "message": "This game does not use %s." % feature}
    if action["type"] in ("buy", "sell") and not card.has("inventory"):
        return {"action": action, "ok": False, "message": "This game does not use inventory."}
    try:
        message = handler(card, state, action)
    except Rejected as e:
        return {"action": action, "ok": False, "message": str(e)}
    return {"action": action, "ok": True, "message": message}


def shop_price(card, state, shop_id, item_id):
    """What the shop charges for one of the item."""
    for entry in card.shops[shop_id].get("stock", []):
        if entry["item"] == item_id and "price" in entry:
            return entry["price"]
    return all_items(card, state)[item_id].get("value", 0)


def sell_price(card, state, shop_id, item_id):
    """What the shop pays for one of the item: half its price, rounded down."""
    return int(shop_price(card, state, shop_id, item_id) // 2)


# Lookups. LLMs sometimes send a display name where an id belongs, so both are accepted.

def _plain(text):
    return "".join(c for c in text.lower() if c.isalnum())


def _find(index, ref, what):
    if isinstance(ref, str):
        if ref in index:
            return ref, index[ref]
        low = ref.strip().lower()
        for key, value in index.items():
            if key.lower() == low or str(value.get("name", value.get("title", ""))).lower() == low:
                return key, value
        # A model that was given a name and writes an id for it, or the other way round: "royal_gardens"
        # for a place the story has just named "Royal Gardens" (whose id is gen_royal_gardens).
        plain = _plain(ref)
        for key, value in index.items():
            if plain and plain in (_plain(key), _plain(key[4:] if key.startswith("gen_") else key), _plain(str(value.get("name", value.get("title", ""))))):
                return key, value
    raise Rejected("There is no %s called %r." % (what, ref))


def _who(state, action, key="who"):
    return _find(state["actors"], action.get(key, PLAYER), "character")


def _item(card, state, action):
    return _find(all_items(card, state), action.get("item"), "item")


def _qty(action):
    qty = action.get("qty", 1)
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty != int(qty) or qty < 1:
        raise Rejected("qty must be a whole number of 1 or more.")
    return int(qty)


def _amount(action):
    amount = action.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise Rejected("amount must be a number.")
    return amount


def _fmt(n):
    return str(int(n)) if n == int(n) else str(n)


def _signed(n):
    return ("+" if n >= 0 else "") + _fmt(n)


def _count(item, qty):
    return item["name"] if qty == 1 else "%s x%d" % (item["name"], qty)


def _need(actor, item_id, item, qty):
    held = actor["inventory"].get(item_id, 0)
    if held == 0:
        raise Rejected("%s does not have %s." % (actor["name"], item["name"]))
    if held < qty:
        raise Rejected("%s only has %d %s." % (actor["name"], held, item["name"]))


def _shed(actor, item_id, qty):
    """What someone is wearing is theirs to lose or hand over too. When what they carry falls short
    by the one they have on, it comes off first, so a slipper that falls from a foot is found."""
    if actor["inventory"].get(item_id, 0) == qty - 1:
        for slot, worn in list(actor["equipment"].items()):
            if worn == item_id:
                del actor["equipment"][slot]
                _give(actor, item_id, 1)
                return


def _give(actor, item_id, qty):
    actor["inventory"][item_id] = actor["inventory"].get(item_id, 0) + qty


def _take(actor, item_id, qty):
    actor["inventory"][item_id] -= qty
    if actor["inventory"][item_id] <= 0:
        del actor["inventory"][item_id]


def _change_stat(card, state, who, stat_id, amount):
    actor = state["actors"][who]
    stat = card.stats[stat_id]
    actor["stats"][stat_id] = clamp_stat(card, actor, stat_id, actor["stats"].get(stat_id, stat["default"]) + amount)
    return "%s %s, now %s" % (stat["name"], _signed(amount), _fmt(effective_stat(card, state, who, stat_id)))


# Handlers. Each one checks everything first and only then changes the state.

def _use_item(card, state, a):
    wid, who = _who(state, a)
    iid, item = _item(card, state, a)
    tid, target = _who(state, a, "target") if "target" in a else (wid, who)
    _need(who, iid, item, 1)
    if item["type"] == "equipment":
        raise Rejected("%s is equipment. Equip it instead of using it." % item["name"])
    if item["type"] != "consumable":
        return "%s uses %s. It is not used up." % (who["name"], item["name"])
    _take(who, iid, 1)
    changes = [_change_stat(card, state, tid, e["stat"], e["amount"]) for e in item.get("effects", [])]
    on = "" if tid == wid else " on %s" % target["name"]
    return "%s uses %s%s%s." % (who["name"], item["name"], on, " (%s)" % "; ".join(changes) if changes else "")


def _equip(card, state, a):
    wid, who = _who(state, a)
    iid, item = _item(card, state, a)
    if iid in who["equipment"].values():
        raise Rejected("%s already has %s equipped." % (who["name"], item["name"]))
    _need(who, iid, item, 1)
    if item["type"] == "misc" and item.get("generated") and a.get("slot") in SLOTS and card.has("equipment"):
        # A plain thing the story made and never said could be worn. Whoever puts it on says where
        # it goes, and from then on it is equipment. It has no effects, so nothing is gained by it.
        _reshape(card, state, item, {"type": "equipment", "slot": a["slot"]})
    if item["type"] != "equipment":
        raise Rejected("%s cannot be equipped." % item["name"])
    slot = item["slot"]
    previous = who["equipment"].get(slot)
    _take(who, iid, 1)
    if previous:
        _give(who, previous, 1)
    who["equipment"][slot] = iid
    return "%s equips %s." % (who["name"], item["name"])


def _unequip(card, state, a):
    wid, who = _who(state, a)
    slot = a.get("slot")
    if slot not in SLOTS and a.get("item") is not None:
        # Named by what comes off rather than where it was worn.
        iid, item = _item(card, state, a)
        slot = dict((worn, s) for s, worn in who["equipment"].items()).get(iid)
        if slot is None:
            raise Rejected("%s does not have %s equipped." % (who["name"], item["name"]))
    if slot not in SLOTS:
        raise Rejected("There is no equipment slot called %r." % (slot,))
    iid = who["equipment"].get(slot)
    if not iid:
        raise Rejected("%s has nothing equipped on %s." % (who["name"], slot))
    del who["equipment"][slot]
    _give(who, iid, 1)
    return "%s takes off %s." % (who["name"], all_items(card, state)[iid]["name"])


def _transfer_item(card, state, a):
    fid, giver = _who(state, a, "from")
    tid, taker = _who(state, a, "to")
    iid, item = _item(card, state, a)
    qty = _qty(a)
    if fid == tid:
        raise Rejected("%s cannot give an item to themselves." % giver["name"])
    if not together(card, giver, taker):
        raise Rejected("%s and %s are not in the same place." % (giver["name"], taker["name"]))
    _shed(giver, iid, qty)
    _need(giver, iid, item, qty)
    _take(giver, iid, qty)
    _give(taker, iid, qty)
    return "%s gives %s to %s." % (giver["name"], _count(item, qty), taker["name"])


def _add_item(card, state, a):
    wid, who = _who(state, a)
    iid, item = _item(card, state, a)
    qty = _qty(a)
    _give(who, iid, qty)
    return "%s gets %s." % (who["name"], _count(item, qty))


def _remove_item(card, state, a):
    wid, who = _who(state, a)
    iid, item = _item(card, state, a)
    qty = _qty(a)
    _shed(who, iid, qty)
    _need(who, iid, item, qty)
    _take(who, iid, qty)
    return "%s loses %s." % (who["name"], _count(item, qty))


def _shape(card, a):
    """What an action says an invented item is: the fields it names, checked, and nothing more.

    A field that makes no sense is left out rather than refused, so a slip in one of them does not
    cost the story its item."""
    shape = {}
    kind = a.get("kind")
    if a.get("slot") in SLOTS and card.has("equipment") and kind in (None, "equipment"):
        shape.update(type="equipment", slot=a["slot"])
    elif kind in ("consumable", "misc"):
        shape["type"] = kind
    if isinstance(a.get("effects"), list):
        effects = []
        for effect in a["effects"]:
            try:
                sid = _find(card.stats, effect.get("stat"), "stat")[0] if isinstance(effect, dict) else None
            except Rejected:
                continue
            amount = effect.get("amount") if sid else None
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount and sid not in [e["stat"] for e in effects]:
                effects.append({"stat": sid, "amount": amount})
        shape["effects"] = effects
    return shape


def _bonus_limit(card, stat_id):
    """The most invented gear may add to a stat or take from it: half the stat's range, or where it
    has no ceiling, its starting value or the most any of the card's own gear gives it. Something
    used up is not held to this; what it does is over at once and stays within the stat's own limits."""
    stat = card.stats[stat_id]
    if stat.get("max") is not None:
        return max((stat["max"] - stat.get("min", 0)) / 2.0, 1)
    given = [abs(e["amount"]) for i in card.items.values() if i["type"] == "equipment" for e in i.get("effects", []) if e["stat"] == stat_id]
    return max([abs(stat["default"]), 1] + given)


def _reshape(card, state, item, shape):
    """Makes an invented item what the story now says it is. Returns whether anything changed."""
    before = dict(item)
    if shape.get("type") == "misc":
        item.pop("effects", None)
    item.update(shape)
    if item.get("effects") and item["type"] == "misc":
        item["type"] = "consumable"                 # it does something and is worn nowhere: it is used
    if item["type"] != "equipment":
        item.pop("slot", None)
    if item["type"] == "equipment" and item.get("effects"):
        item["effects"] = [{"stat": e["stat"], "amount": max(-_bonus_limit(card, e["stat"]), min(_bonus_limit(card, e["stat"]), e["amount"]))}
                           for e in item["effects"]]
    if not item.get("effects"):
        item.pop("effects", None)
    # One being worn somewhere it no longer belongs goes back among its owner's things.
    for actor in state["actors"].values():
        for slot, worn in list(actor["equipment"].items()):
            if worn == item["id"] and item.get("slot") != slot:
                del actor["equipment"][slot]
                _give(actor, worn, 1)
    return item != before


def _what(card, item):
    does = ", ".join("%s %s" % (card.stats[e["stat"]]["name"], _signed(e["amount"])) for e in item.get("effects", []))
    kind = {"equipment": "worn or held (%s)" % item.get("slot"), "consumable": "used up when used"}.get(item["type"], "a plain item")
    return kind + ("; %s" % does if does else "")


def _create_item(card, state, a):
    wid, who = _who(state, a)
    qty = _qty(a)
    name = a.get("name")
    if not isinstance(name, str) or not name.strip():
        raise Rejected("A new item needs a name.")
    name = name.strip()
    items = all_items(card, state)
    shape = _shape(card, a)
    try:
        iid, item = _find(items, name, "item")
    except Rejected:
        iid = None
    if iid is None:
        if not card.allow_generated_items:
            raise Rejected("This game only has the items its card defines; %r is not one of them." % name)
        base = "gen_" + ("".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "item")
        iid, n = base, 2
        while iid in items:
            iid, n = "%s_%d" % (base, n), n + 1
        item = {"id": iid, "name": name, "description": str(a.get("description", "")), "type": "misc", "generated": True}
        _reshape(card, state, item, shape)
        state["generated_items"][iid] = item
    elif shape and item.get("generated"):
        # An item the story made earlier, said again as something else: the one already held
        # becomes that, and no second one is handed over.
        held = who["inventory"].get(iid) or iid in who["equipment"].values()
        if _reshape(card, state, item, shape) and held:
            return "%s is now %s." % (item["name"], _what(card, item))
    _give(who, iid, qty)
    return "%s gets %s." % (who["name"], _count(item, qty))


def _change_item(card, state, a):
    """An item the story made becomes something else: a rag sewn into a hood, a vial that turns
    out to heal, a blade that has lost its edge. The card's own items stay as the card made them."""
    iid, item = _item(card, state, a)
    if not item.get("generated"):
        raise Rejected("%s is one of this game's own items and stays as it is." % item["name"])
    if a.get("kind") == "equipment" or a.get("slot") is not None:
        if not card.has("equipment"):
            raise Rejected("This game does not use equipment.")
        if a.get("slot") not in SLOTS:
            raise Rejected("Something worn or held needs a slot (one of %s)." % ", ".join(SLOTS))
    words = {}
    name = a.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != item["name"]:
        if len(name) > 60 or any(_plain(other["name"]) == _plain(name) for oid, other in all_items(card, state).items() if oid != iid):
            raise Rejected("%r cannot be its name: too long, or another item is called that." % name.strip())
        words["name"] = name.strip()
    if isinstance(a.get("description"), str) and a["description"] != item.get("description", ""):
        words["description"] = a["description"]
    was = item["name"]
    if not (_reshape(card, state, item, _shape(card, a)) | bool(words)):
        raise Rejected("%s is already that." % was)
    item.update(words)
    return "%s is now %s%s." % (was, "called %s, " % item["name"] if "name" in words else "", _what(card, item))


def _shop(card, state, a):
    sid, shop = _find(card.shops, a.get("shop"), "shop")
    player = state["actors"][PLAYER]
    if shop.get("location") and player["location"] != shop["location"]:
        raise Rejected("%s is not at %s." % (player["name"], shop["name"]))
    return sid, shop, player


def _buy(card, state, a):
    sid, shop, player = _shop(card, state, a)
    iid, item = _item(card, state, a)
    qty = _qty(a)
    stock = state["shops"][sid]
    if iid not in stock:
        raise Rejected("%s does not sell %s." % (shop["name"], item["name"]))
    left = stock[iid]
    if left is not None and left < qty:
        raise Rejected("%s is sold out of %s." % (shop["name"], item["name"]) if left == 0
                       else "%s only has %d %s left." % (shop["name"], left, item["name"]))
    cost = shop_price(card, state, sid, iid) * qty
    if player["money"] < cost:
        raise Rejected("%s costs %s %s but %s only has %s." % (_count(item, qty), _fmt(cost), card.currency, player["name"], _fmt(player["money"])))
    player["money"] -= cost
    if shop.get("keeper"):
        state["actors"][shop["keeper"]]["money"] += cost
    if left is not None:
        stock[iid] = left - qty
    _give(player, iid, qty)
    return "%s buys %s from %s for %s %s." % (player["name"], _count(item, qty), shop["name"], _fmt(cost), card.currency)


def _sell(card, state, a):
    sid, shop, player = _shop(card, state, a)
    iid, item = _item(card, state, a)
    qty = _qty(a)
    _need(player, iid, item, qty)
    pay = sell_price(card, state, sid, iid) * qty
    if pay <= 0:
        raise Rejected("%s will not buy %s." % (shop["name"], item["name"]))
    _take(player, iid, qty)
    player["money"] += pay
    stock = state["shops"][sid]
    if stock.get(iid) is not None:
        stock[iid] += qty
    return "%s sells %s to %s for %s %s." % (player["name"], _count(item, qty), shop["name"], _fmt(pay), card.currency)


def _change_money(card, state, a):
    wid, who = _who(state, a)
    amount = _amount(a)
    if who["money"] + amount < 0:
        raise Rejected("%s only has %s %s." % (who["name"], _fmt(who["money"]), card.currency))
    who["money"] += amount
    return "%s %s %s %s, now %s." % (who["name"], "gains" if amount >= 0 else "loses", _fmt(abs(amount)), card.currency, _fmt(who["money"]))


def _change_stat_action(card, state, a):
    wid, who = _who(state, a)
    sid, stat = _find(card.stats, a.get("stat"), "stat")
    return "%s: %s." % (who["name"], _change_stat(card, state, wid, sid, _amount(a)))


def _change_stat_max(card, state, a):
    """Lasting growth or loss the story shows: the most someone can have of a stat changes. What
    they have now goes up with it, the way it does on a new level, and never stays above it."""
    wid, who = _who(state, a)
    sid, stat = _find(card.stats, a.get("stat"), "stat")
    amount = _amount(a)
    ceiling = stat_max(card, who, sid)
    if ceiling is None:
        raise Rejected("%s has no maximum to change. Change the stat itself." % stat["name"])
    if amount == 0:
        raise Rejected("amount must not be zero.")
    who.setdefault("max", {})[sid] = max(ceiling + amount, stat_min(card, who, sid))
    now = who["stats"].get(sid, stat["default"])
    who["stats"][sid] = clamp_stat(card, who, sid, now + max(amount, 0))
    return "%s: most %s %s, now %s/%s." % (who["name"], stat["name"], _signed(amount), _fmt(effective_stat(card, state, wid, sid)), _fmt(who["max"][sid]))


def _connected(a, b):
    return b["id"] in a.get("connections", []) or a["id"] in b.get("connections", [])


def _move(card, state, a):
    """Puts someone in a location. Connections are not checked here: this is the story moving
    people, and the story can take anyone anywhere (a portal, a journey, an arrest). The limit to
    neighbouring places applies only to where the player may go by their own choice, above."""
    wid, who = _who(state, a)
    lid, location = _find(places(card, state), a.get("location"), "location")
    here = places(card, state).get(who["location"])
    if here and lid == here["id"]:
        raise Rejected("%s is already at %s." % (who["name"], location["name"]))
    who["location"] = lid
    if here:
        left_place(state, here["id"])
    freed = ""
    if wid == PLAYER:
        reveal(card, state, [lid])      # being taken somewhere puts it on the map
        state["scene"] = ""             # what was going on belonged to the place they left
        if state.get("travel_lock") is not None:
            # Whatever held them was holding them there. They are somewhere else now, so it holds them
            # no longer; the story closes the map again if they are still not free (a prisoner being moved).
            state["travel_lock"] = None
            freed = " Nothing holds them there any more."
    if here:
        # Naming where they left lets the narrator weigh what leaving means, and send them back if someone would have stopped them.
        return "%s leaves %s and goes to %s.%s" % (who["name"], here["name"], location["name"], freed)
    return "%s goes to %s.%s" % (who["name"], location["name"], freed)


def _quest_start(card, state, a):
    qid, quest = _find(card.quests, a.get("quest"), "quest")
    if qid in state["quests"]:
        raise Rejected("Quest %s was already started." % quest["title"])
    if quest.get("after") and state["quests"].get(quest["after"], {}).get("status") != "done":
        raise Rejected("Quest %s cannot begin until %s is finished." % (quest["title"], card.quests[quest["after"]]["title"]))
    state["quests"][qid] = {"status": "active", "stage": 0}
    state["quests"][qid]["met"] = quest_marks(card, state, quest)
    return "Quest started: %s. Objective: %s" % (quest["title"], quest["stages"][0]["description"])


def _active_quest(card, state, a):
    qid, quest = _find(card.quests, a.get("quest"), "quest")
    progress = state["quests"].get(qid)
    if not progress or progress["status"] != "active":
        raise Rejected("Quest %s is not active." % quest["title"])
    return quest, progress


REWARD_MEMORY = 15      # turns for which a reward handed over in the story is taken to be the one the game already paid


def paid_lately(state):
    """Quest rewards the game paid a short while ago and the story has not yet been seen to hand
    over: [{"quest", "turn", "money", "stats": {id: amount}, "items": {id: qty}}]."""
    return [p for p in state.get("paid", []) if state["turn"] - p["turn"] <= REWARD_MEMORY and (p["money"] or p["stats"] or p["items"])]


def _paid_already(card, state, action):
    """A quest's reward is paid by the game the moment the quest is finished. The story often shows
    it being handed over afterwards (a ceremony, a purse pushed across the desk), and whoever
    records the story may then report the same reward a second time. So a gain by the player that
    is exactly a reward just paid, the same thing in the same amount, is that reward and is not
    given again. It is only held back once. Any other amount is something else and goes through.

    Returns what to tell the player, or None when the action is to be applied."""
    kind = action.get("type")
    try:
        if kind not in ("change_money", "change_stat", "add_item", "create_item") or _who(state, action)[0] != PLAYER:
            return None
        if kind == "change_money":
            where, key, amount = "money", None, _amount(action)
        elif kind == "change_stat":
            where, key, amount = "stats", _find(card.stats, action.get("stat"), "stat")[0], _amount(action)
        else:
            where, key, amount = "items", _find(all_items(card, state), action.get("item" if kind == "add_item" else "name"), "item")[0], _qty(action)
    except Rejected:
        return None
    for paid in paid_lately(state):
        if amount > 0 and amount == (paid["money"] if key is None else paid[where].get(key, 0)):
            if key is None:
                paid["money"] = 0
            else:
                del paid[where][key]
            return "Already given as the reward for %s." % paid["quest"]
    return None


def _quest_advance(card, state, a):
    quest, progress = _active_quest(card, state, a)
    current = quest["stages"][progress["stage"]]
    if a.get("stage") is not None and a["stage"] != current["id"]:
        # Naming the objective proves the model is talking about the one in progress, not one it expects later.
        raise Rejected("Quest %s is not on that objective. Its current objective is %s." % (quest["title"], current["id"]))
    if game_checked(card, current):
        raise Rejected("The game itself marks that objective of %s once its condition is met." % quest["title"])
    return _next_objective(card, state, quest, progress)


def _next_objective(card, state, quest, progress):
    progress["stage"] += 1
    if progress["stage"] < len(quest["stages"]):
        return "Quest %s: new objective: %s" % (quest["title"], quest["stages"][progress["stage"]]["description"])
    progress["status"] = "done"
    player = state["actors"][PLAYER]
    rewards = quest.get("rewards", {})
    got = []
    if rewards.get("money") and card.has("money"):
        player["money"] += rewards["money"]
        got.append("%s %s" % (_fmt(rewards["money"]), card.currency))
    for stack in rewards.get("items", []) if card.has("inventory") else []:
        _give(player, stack["item"], stack.get("qty", 1))
        got.append(_count(card.items[stack["item"]], stack.get("qty", 1)))
    for stat_id, amount in rewards.get("stats", {}).items():
        got.append(_change_stat(card, state, PLAYER, stat_id, amount))
    if got:
        state.setdefault("paid", []).append({
            "quest": quest["title"], "turn": state["turn"], "money": rewards.get("money", 0) if card.has("money") else 0,
            "stats": dict((stat_id, amount) for stat_id, amount in rewards.get("stats", {}).items() if amount > 0),
            "items": dict((stack["item"], stack.get("qty", 1)) for stack in rewards.get("items", [])) if card.has("inventory") else {}})
        state["paid"] = state["paid"][-5:]
    return "Quest completed: %s.%s" % (quest["title"], " Reward: %s." % ", ".join(got) if got else "")


def settle_quests(card, state):
    """Moves quests on by the conditions the game can check itself (an objective's done_if), the
    way an ordinary game would: holding the item or reaching the place is enough, whatever the
    story said or did not say. Returns the results to show.

    The current objective is finished when its conditions hold. A later objective whose conditions
    have just come true carries the quest past everything before it, so getting the letter early
    does not leave the quest asking where the courier went. "Just come true" matters: a condition
    that was already true when the quest began (being at the inn) skips nothing.
    """
    results = []
    for quest_id, progress in state["quests"].items():
        quest = card.quests.get(quest_id)
        while quest is not None and progress["status"] == "active":
            now = quest_marks(card, state, quest)
            fresh = set(now) - set(progress.get("met", now))
            progress["met"] = now
            stages, reached = quest["stages"], None
            if stages[progress["stage"]]["id"] in now:
                reached = progress["stage"]
            for index in range(progress["stage"] + 1, len(stages)):
                if stages[index]["id"] in fresh:
                    reached = index
            if reached is None:
                break
            progress["stage"] = reached
            results.append({"action": {"type": "quest_advance", "quest": quest_id}, "ok": True,
                            "message": _next_objective(card, state, quest, progress)})
    return results


def _quest_fail(card, state, a):
    quest, progress = _active_quest(card, state, a)
    progress["status"] = "failed"
    return "Quest failed: %s." % quest["title"]


def _gain_xp(card, state, a):
    wid, who = _who(state, a)
    amount = _amount(a)
    if amount <= 0:
        raise Rejected("Experience gained must be above zero.")
    who["xp"] += amount
    news = ["%s gains %s experience" % (who["name"], _fmt(amount))]
    while who["xp"] >= xp_needed(card, who):
        who["xp"] -= xp_needed(card, who)
        who["level"] += 1
        gains = []
        for stat_id, gain in sorted(card.level_gains.items()):
            ceiling = stat_max(card, who, stat_id)
            if ceiling is not None:
                who["max"][stat_id] = ceiling + gain
            who["stats"][stat_id] = clamp_stat(card, who, stat_id, who["stats"].get(stat_id, card.stats[stat_id]["default"]) + gain)
            gains.append("%s %s" % (card.stats[stat_id]["name"], _signed(gain)))
        news.append("reaches level %d%s" % (who["level"], " (%s)" % ", ".join(gains) if gains else ""))
        for skill_id, known in sorted(who["skills"].items()):
            if not known["unlocked"] and known.get("unlock_level") and known["unlock_level"] <= who["level"]:
                known["unlocked"] = True
                news.append("learns %s" % card.skills[skill_id]["name"])
    return ", ".join(news) + "."


def _use_skill(card, state, a):
    wid, who = _who(state, a)
    sid, skill = _find(card.skills, a.get("skill"), "skill")
    known = who["skills"].get(sid)
    if not known:
        raise Rejected("%s does not know %s." % (who["name"], skill["name"]))
    if not known["unlocked"]:
        raise Rejected("%s has not unlocked %s yet." % (who["name"], skill["name"]))
    if skill.get("target", "other") == "self":
        tid, target = wid, who
    elif "target" not in a:
        raise Rejected("%s needs a target." % skill["name"])
    else:
        tid, target = _who(state, a, "target")
        if not together(card, who, target):
            raise Rejected("%s and %s are not in the same place." % (who["name"], target["name"]))
    for stat_id, cost in skill.get("cost", {}).items():
        if who["stats"].get(stat_id, 0) < cost:
            raise Rejected("%s does not have enough %s for %s (needs %s, has %s)." % (
                who["name"], card.stats[stat_id]["name"], skill["name"], _fmt(cost), _fmt(who["stats"].get(stat_id, 0))))
    changes = [_change_stat(card, state, wid, stat_id, -cost) for stat_id, cost in sorted(skill.get("cost", {}).items())]
    effects = [_change_stat(card, state, tid, e["stat"], e["amount"]) for e in skill.get("effects", [])]
    on = "" if tid == wid else " on %s" % target["name"]
    told = []
    if changes:
        told.append("cost: " + "; ".join(changes))
    if effects:
        told.append(("effect: " if tid == wid else "%s: " % target["name"]) + "; ".join(effects))
    return "%s uses %s%s%s." % (who["name"], skill["name"], on, " (%s)" % " | ".join(told) if told else "")


def _unlock_skill(card, state, a):
    wid, who = _who(state, a)
    sid, skill = _find(card.skills, a.get("skill"), "skill")
    known = who["skills"].setdefault(sid, {"unlocked": False, "unlock_level": None})
    if known["unlocked"]:
        raise Rejected("%s already knows %s." % (who["name"], skill["name"]))
    known["unlocked"] = True
    return "%s learns %s." % (who["name"], skill["name"])


def _change_relationship(card, state, a):
    wid, who = _who(state, a)
    if wid == PLAYER:
        raise Rejected("%s is tracked for other characters, not the player." % card.relationship_name)
    amount = _amount(a)
    who["relationship"] = max(0, min(100, who["relationship"] + amount))
    return "%s's %s %s, now %s/100." % (who["name"], card.relationship_name.lower(), _signed(amount), _fmt(who["relationship"]))


def _says_it_ended(name, note):
    """Whether a note written on a state someone already has says that this very state is over.
    "no longer charging" on "Charging at Sekke" does; "tied to a chair, no longer gagged" on
    "Restrained" does not, since it is the gag that ended, not the ropes."""
    note = note.strip().lower()
    if re.match(r"^(it |this )?(is |has )?(now )?(over|ended|gone|cleared|none|no longer|not any ?more)\W*$", note):
        return True
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) >= 4]
    stems = "|".join(re.escape(w[:max(4, len(w) - 3)]) for w in words)        # "charging" also answers to "charge", "charged"
    return bool(stems) and bool(re.search(r"\b(no longer|not any ?more|stopped|done)\b[\w ]{0,20}?\b(%s)" % stems, note))



def _set_state(card, state, a):
    wid, who = _who(state, a)
    ref = a.get("state")
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 40:
        raise Rejected("A state needs a short name.")
    try:
        sid, known = _find(card.states, ref, "state")
    except Rejected:
        # A state the card never defined is still worth remembering; it just stops nothing.
        sid = "".join(c if c.isalnum() else "_" for c in ref.strip().lower()).strip("_") or "state"
    note = a.get("note") if isinstance(a.get("note"), str) else ""
    if sid in who["states"] and _says_it_ended(who["states"][sid]["name"], note):
        # Some models "update" a state to say it has ended instead of clearing it. Take it as meant:
        # the state is removed, not kept with the note.
        name = who["states"].pop(sid)["name"]
        return "%s is no longer %s." % (who["name"], name.lower())
    who["states"][sid] = {"name": state_name(card, sid), "note": note.strip()[:200]}
    return "%s is now %s%s." % (who["name"], who["states"][sid]["name"].lower(), " (%s)" % who["states"][sid]["note"] if note.strip() else "")


def _clear_state(card, state, a):
    wid, who = _who(state, a)
    ref = a.get("state")
    held = dict((sid, h) for sid, h in who["states"].items())
    match = [sid for sid, h in held.items() if isinstance(ref, str) and ref.strip().lower() in (sid, h["name"].lower())]
    if not match:
        raise Rejected("%s is not %s." % (who["name"], ref))
    name = who["states"].pop(match[0])["name"]
    return "%s is no longer %s." % (who["name"], name.lower())


def _create_location(card, state, a):
    """A place the story needs that the card never defined: a pocket dimension, a roadside camp,
    wherever the demon god threw everyone. It has no connections unless one is named, so the only
    way in or out is the way the story provides."""
    if not card.data["rules"].get("allow_generated_locations", True):
        raise Rejected("This game's map is fixed; the story cannot add places to it.")
    name = a.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise Rejected("A new place needs a short name.")
    name = name.strip()
    try:
        lid, existing = _find(places(card, state), name, "location")
        reveal(card, state, [lid])
        return "%s is already a place on the map." % existing["name"]
    except Rejected:
        pass
    base = "gen_" + ("".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "place")
    lid, n = base, 2
    while lid in places(card, state):
        lid, n = "%s_%d" % (base, n), n + 1
    place = {"id": lid, "name": name, "description": str(a.get("description") or "")[:400], "connections": [],
             "generated": True, "temporary": a.get("temporary") is True}
    if a.get("connected_to") is not None:
        place["connections"] = [_find(places(card, state), a["connected_to"], "location")[0]]
    state.setdefault("generated_locations", {})[lid] = place
    reveal(card, state, [lid])
    return "A new place: %s%s." % (name, ", for as long as someone is there" if place["temporary"] else "")


def _reveal_location(card, state, a):
    lid, location = _find(places(card, state), a.get("location"), "location")
    if not reveal(card, state, [lid]):
        raise Rejected("%s is already on the map." % location["name"])
    return "The map now shows %s." % location["name"]


def _lock_travel(card, state, a):
    reason = a.get("reason") if isinstance(a.get("reason"), str) and a["reason"].strip() else "something is keeping them here."
    state["travel_lock"] = reason.strip()[:200]
    return "%s can no longer leave: %s" % (state["actors"][PLAYER]["name"], state["travel_lock"])


def _unlock_travel(card, state, a):
    if state.get("travel_lock") is None:
        raise Rejected("%s is already free to travel." % state["actors"][PLAYER]["name"])
    state["travel_lock"] = None
    return "%s is free to travel again." % state["actors"][PLAYER]["name"]


def _start_battle(card, state, a):
    if not card.battle_system:
        raise Rejected("Fights in this game are told in the story, not run by the game.")
    player = state["actors"][PLAYER]
    health = card.battle["health_stat"]
    refs = a.get("enemies")
    refs = [refs] if isinstance(refs, str) else refs
    if not isinstance(refs, list) or not refs:
        raise Rejected("A fight needs at least one enemy.")
    enemies = []
    for ref in refs:
        eid, enemy = _find(state["actors"], ref, "character")
        if eid == PLAYER:
            raise Rejected("%s cannot fight themselves." % player["name"])
        if enemy["stats"].get(health, 0) <= 0:
            raise Rejected("%s is in no state to fight." % enemy["name"])
        if not together(card, enemy, player):
            raise Rejected("%s is not here." % enemy["name"])
        if eid not in enemies:
            enemies.append(eid)
    for eid in enemies:
        # An enemy the card keeps off the map walks into the scene when its fight starts.
        state["actors"][eid]["location"] = player["location"]
    state["battle"] = {"enemies": enemies, "round": 1}
    return "A fight begins: %s against %s." % (player["name"], ", ".join(state["actors"][e]["name"] for e in enemies))


_HANDLERS = {
    "use_item": _use_item,
    "equip": _equip,
    "unequip": _unequip,
    "transfer_item": _transfer_item,
    "add_item": _add_item,
    "remove_item": _remove_item,
    "create_item": _create_item,
    "change_item": _change_item,
    "buy": _buy,
    "sell": _sell,
    "change_money": _change_money,
    "change_stat": _change_stat_action,
    "change_stat_max": _change_stat_max,
    "move": _move,
    "quest_start": _quest_start,
    "quest_advance": _quest_advance,
    "quest_fail": _quest_fail,
    "gain_xp": _gain_xp,
    "use_skill": _use_skill,
    "unlock_skill": _unlock_skill,
    "change_relationship": _change_relationship,
    "start_battle": _start_battle,
    "create_location": _create_location,
    "reveal_location": _reveal_location,
    "lock_travel": _lock_travel,
    "unlock_travel": _unlock_travel,
    "set_state": _set_state,
    "clear_state": _clear_state,
}
