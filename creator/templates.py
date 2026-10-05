"""Starting points for a new card. A template only pre-fills rules; everything stays editable."""

import re

RPG_STATS = [
    {"id": "hp", "name": "Health", "min": 0, "max": 20, "default": 20},
    {"id": "mana", "name": "Mana", "min": 0, "max": 10, "default": 10},
    {"id": "stamina", "name": "Stamina", "min": 0, "max": 10, "default": 10},
    {"id": "attack", "name": "Attack", "min": 0, "default": 0},
    {"id": "defense", "name": "Defense", "min": 0, "default": 0},
]

# What "Casual" and "Classic RPG" mean. Also offered as presets inside the editor.
RULES = {
    "blank": {"stats": []},
    "casual": {
        "stats": [],
        "features": {"inventory": False, "equipment": False, "money": False, "levels": False, "skills": False, "relationships": True, "states": True},
        "relationship_name": "Affection",
    },
    "rpg": {
        "stats": RPG_STATS,
        "features": {"inventory": True, "equipment": True, "money": True, "levels": True, "skills": True, "relationships": False, "states": True},
        "leveling": {"xp_per_level": 100, "gains": {"hp": 5, "mana": 2}},
        "battle": {"mode": "system", "health_stat": "hp", "attack_stat": "attack", "defense_stat": "defense",
                   "basic_damage": 2, "on_defeat": {"type": "survive", "health": 1}},
        "currency_name": "Gold",
    },
}


def slug(text, fallback="card"):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or fallback


def new_card(template, title):
    rules = RULES.get(template, RULES["blank"])
    return {
        "spec": "aigame-card",
        "spec_version": "0.1",
        "meta": {"id": slug(title), "title": title, "version": "0.1.0"},
        "world": {
            "description": "Describe the setting and its tone here.",
            "opening": "Write the first thing the player reads here. Use {{user}} for the player's name.",
        },
        "rules": rules,
        "characters": [],
    }
