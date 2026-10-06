# SI-Station

SI-Station (Superintelligence Station) is a console for AI-told story games. The game itself knows
nothing about any particular story: you insert a **card**, and the card supplies the world, the
characters, the art and the rules. An AI model of your choice narrates, and the engine keeps
track of everything that has to stay true, such as what you are carrying, where everyone is and
which quests are open.

It has three parts, all in this repository:

- **The game** (`game/`), built on [Ren'Py](https://www.renpy.org/).
- **The card creator** (`creator/`), a small local web app for making cards.
- **An MCP server** (`creator/mcp_server.py`), so an AI assistant such as Claude Code can make
  and edit cards for you.

## Playing

1. Install the Ren'Py SDK (8.5 or later) and open this folder as a project in its launcher.
2. Launch it, choose a card, then open **Models** and pick a provider, enter your API key and
   choose a model. Anthropic, OpenRouter, Nano-GPT, any OpenAI-compatible address and local
   models are supported. A second, cheaper model can be set for small background tasks.
3. Press **Start**.

Two sample cards are included: *The Rusty Lantern*, a full RPG with stats, skills, levels and
turn-based fights, and *Quiet Hours*, a slice-of-life card that tracks nothing but friendships.

Your API keys are handed to your operating system's own store for secrets (the Keychain on
macOS, an account-encrypted file on Windows, the desktop's secret service on Linux) and are never
written into this project, a card, a save or the game's saved settings. Where a system has no such
store, they go into a private file outside the game folder that is scrambled, not encrypted.

## Making cards

```
python3 creator/server.py
```

This opens the creator in your browser. It edits card folders in `cards/`, so a card you are
working on can be inserted in the game straight away. Every system is optional per card:
inventory, equipment, money, stats, levels, skills, relationships, states, quests, a map, shops,
and whether fights are told by the story or played turn by turn. The creator can also import
SillyTavern character cards and lorebooks.

When a card is finished, **Export** packs it into a single `.sicard` file to share. From a
terminal, `python3 tools/pack_card.py cards/your_card` does the same.

### About `.sicard` files

A `.sicard` holds a whole card, pictures included, sealed into one file that ordinary tools do
not open. Treat that as tidy packaging and a first layer of privacy, not as a lock: SI-Station is
open source, so how cards are sealed is public, and the story text is shared with whichever AI
model the player uses. If you are sharing art you need to keep control of, keep that in mind.

The sealing lives in two small functions, `seal` and `unseal` in `game/aigame/card.py`. That is
deliberate: it is a plain starting point that a fork can build on for its own needs without
changing anything else. None of this is a reason not to use SI-Station exactly as it is.

## Editing cards with an AI assistant

`.mcp.json` registers the MCP server with Claude Code when you open this folder; approve it once
when asked. For other MCP clients, the command is `python3 creator/mcp_server.py`. The assistant
can then list, read, create and edit cards, add images, import lorebooks and pack cards, and is
told after every change what the game's own checks make of the result.

## For developers

```
python3 -m unittest discover tests
```

The game logic in `game/aigame/` is plain Python with no Ren'Py imports, which is what lets it be
tested directly and shared with the creator. The card, preset, action and settings formats are
described in `spec/`. `tests/test_in_step.py` fails when the engine, those format files, the
creator and the MCP server stop agreeing with each other.

Nothing here needs installing beyond Python 3.10 or later and the Ren'Py SDK.

## Licence

SI-Station is free software under the [GNU Affero General Public License, version 3](LICENSE).
You may use, change and share it, including commercially, as long as anything you distribute or
run as a service for others stays under the same licence with its source available.

The licence covers the program. Cards are your own work: a card you make, and the art and writing
in it, are yours to license however you like.

Copyright (C) 2026 nirwannrs
