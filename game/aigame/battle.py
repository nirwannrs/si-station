"""Turn-based fights for cards whose rules.battle.mode is "system".

A fight starts with the start_battle action and is then played in rounds from the battle screen:
the player picks one thing to do (or waits), then every enemy still standing acts. A state that
blocks attacking, such as asleep or restrained, costs a fighter their turn. Enemies follow a simple
rule: half the time a damaging skill they can afford, otherwise a plain attack. The model is not
called during a fight; it narrates the aftermath from the results.
"""

import random

from .actions import apply_action, _change_stat
from .card import PLAYER
from .state import blocked, effective_stat, is_away

_rng = random.Random()


def _health(card, state, who):
    return state["actors"][who]["stats"].get(card.battle["health_stat"], 0)


def standing(card, state):
    """The enemies still able to fight."""
    return [e for e in state["battle"]["enemies"] if _health(card, state, e) > 0]


def attack_damage(card, state, attacker, target):
    """A plain attack: the card's basic damage, plus the attacker's attack stat, less the target's defense. Never under 1."""
    rules = card.battle
    damage = rules.get("basic_damage", 2)
    if rules.get("attack_stat"):
        damage += effective_stat(card, state, attacker, rules["attack_stat"])
    if rules.get("defense_stat"):
        damage -= effective_stat(card, state, target, rules["defense_stat"])
    return max(1, damage)


def _result(ok, message, **action):
    return {"action": action, "ok": ok, "message": message}


def _attack(card, state, attacker, target):
    change = _change_stat(card, state, target, card.battle["health_stat"], -attack_damage(card, state, attacker, target))
    names = state["actors"][attacker]["name"], state["actors"][target]["name"]
    return _result(True, "%s attacks %s (%s)." % (names[0], names[1], change), type="battle_attack", who=attacker, target=target)


def _enemy_skills(card, state, enemy):
    """Skills the enemy could use on the player right now to hurt them."""
    actor = state["actors"][enemy]
    usable = []
    for skill_id, known in sorted(actor["skills"].items()):
        skill = card.skills[skill_id]
        hurts = any(e["stat"] == card.battle["health_stat"] and e["amount"] < 0 for e in skill.get("effects", []))
        affordable = all(actor["stats"].get(stat, 0) >= cost for stat, cost in skill.get("cost", {}).items())
        if known["unlocked"] and skill.get("target", "other") == "other" and hurts and affordable:
            usable.append(skill_id)
    return usable


def take_turn(card, state, action, rng=_rng):
    """Plays one round and returns its results. A rejected player action costs nothing: no enemy acts."""
    kind = action.get("type") if isinstance(action, dict) else None
    player = state["actors"][PLAYER]

    stopped_by = blocked(card, player, {"battle_attack": "attack", "battle_flee": "move"}.get(kind, ""))
    if stopped_by:
        return [_result(False, "%s cannot do that while %s." % (player["name"], stopped_by.lower()), **action)]

    if kind == "battle_wait":
        results = [_result(True, "%s holds back." % player["name"], **action)]
    elif kind == "battle_attack":
        if action.get("target") not in standing(card, state):
            return [_result(False, "That is not an enemy still standing.", **action)]
        results = [_attack(card, state, PLAYER, action["target"])]
    elif kind == "battle_flee":
        if rng.random() < 0.5:
            state["battle"] = None
            return [_result(True, "%s gets away from the fight." % player["name"], **action)]
        results = [_result(False, "%s tries to get away but cannot." % player["name"], **action)]
    elif kind in ("use_skill", "use_item"):
        results = [apply_action(card, state, dict(action, who=PLAYER), by_player=True)]
        if not results[0]["ok"]:
            return results
    else:
        return [_result(False, "That cannot be done in a fight.")]

    if not standing(card, state):
        beaten = state["battle"]["enemies"]
        results.append(_result(True, "The fight is won. Defeated: %s." % ", ".join(state["actors"][e]["name"] for e in beaten)))
        reward = sum(card.characters[e].get("xp_reward", 0) for e in beaten if e in card.characters)
        state["battle"] = None
        if reward and card.has("levels"):
            results.append(apply_action(card, state, {"type": "gain_xp", "amount": reward}))
        return results

    for enemy in standing(card, state):
        actor = state["actors"][enemy]
        if is_away(card, actor):
            continue
        idle = blocked(card, actor, "attack")
        skills = [] if blocked(card, actor, "skills") else _enemy_skills(card, state, enemy)
        if idle and not skills:
            results.append(_result(True, "%s is %s and does nothing." % (actor["name"], idle.lower())))
        elif skills and (idle or rng.random() < 0.5):
            results.append(apply_action(card, state, {"type": "use_skill", "who": enemy, "skill": rng.choice(skills), "target": PLAYER}))
        else:
            results.append(_attack(card, state, enemy, PLAYER))
        if _health(card, state, PLAYER) <= 0:
            break

    if _health(card, state, PLAYER) <= 0:
        defeat = card.battle.get("on_defeat", {})
        state["battle"] = None
        if defeat.get("type", "survive") == "game_over":
            state["game_over"] = True
            results.append(_result(True, "%s is defeated. The story ends here." % player["name"]))
        else:
            health = card.stats[card.battle["health_stat"]]
            player["stats"][health["id"]] = max(1, defeat.get("health", 1))
            where = card.locations.get(defeat.get("location"))
            if where:
                player["location"] = where["id"]
            results.append(_result(True, "%s is beaten and left for dead, and comes round later%s with %s %s." % (
                player["name"], " at " + where["name"] if where else "", player["stats"][health["id"]], health["name"])))
        return results

    state["battle"]["round"] += 1
    return results
