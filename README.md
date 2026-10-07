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

SI-Station is in beta. The downloads on the [Releases](../../releases) page are pre-releases:
they are played and tested on macOS, and built but not yet tried by anyone on Windows and Linux.

## What the game does

- **Keeps the story true.** The engine, not the model, holds what everyone carries, their stats,
  where they are, the states they are in and how far each quest has got. Things are recorded only
  when the story shows them happen, not when someone talks about them.
- **Uses small helper calls around the story model.** One reads what you typed, one records what
  each reply changed, one judges quests, one works out who is speaking and where people are, and
  one moves the people you know around the world while you are elsewhere. They can run on a
  second, cheaper model.
- **Remembers long stories.** Old turns are folded into a running summary, and a journal keeps an
  entry for each scene that is sent again only when it matters. Every past turn says who was
  present, so characters do not know what they were not there for.
- **Keeps the story's own clock.** A line with the time, date, exact spot and weather is kept
  going by the story model and shown at the top of the screen. A card can give it its world's
  calendar.
- **Plays with pictures or as text.** A card can have backgrounds and character art, or be
  text-only with the story filling the screen.
- **Lets you take turns back.** Undo restores the game exactly as it was, up to ten turns.

## Playing

1. Download the zip for your system from the [Releases](../../releases) page and unzip it.
   Nothing else needs installing.
   - **Windows:** run `SIStation.exe`.
   - **macOS:** open `SIStation.app`. The app is not signed with an Apple developer certificate,
     so the first time macOS says it "could not verify SIStation is free of malware" and will
     not open it. Click **Done**, then go to **System Settings > Privacy & Security**, scroll to
     the Security section and click **Open Anyway** next to SIStation. Open the app once more
     and confirm. This is needed only once. (Or, in Terminal:
     `xattr -dr com.apple.quarantine /path/to/SIStation.app`.)
   - **Linux:** run `SIStation.sh`.
2. Choose a card, then open **Menu > Settings > Models**, pick a provider, enter your API key and
   choose a model. Anthropic, OpenRouter, Nano-GPT, any OpenAI-compatible address and local
   models are supported. A second, cheaper model can be set for small background tasks.
3. Press **Start**.

**Menu > Settings** also has **Parameters** (response length, context size, and switches for the
bookkeeper, the journal, the time and place line, the world moving on, and a check a thinking
model runs before it writes), **Preset** and **Persona**, where you can write the character you
play as for every card.

The card creator comes with the game: press **Card creator** on the main menu and it opens in
your browser. To go straight to it, start `SI-Station Creator.bat` (Windows) or
`si-station-creator` (Linux) instead of the game.

Your own cards and presets are kept outside the game's folder, so a newer download never
touches them: `%APPDATA%\SI-Station` on Windows, `~/Library/Application Support/SI-Station` on
macOS, `~/.local/share/si-station` on Linux.

To run from source instead, install the Ren'Py SDK (8.5 or later) and open this folder as a
project in its launcher. Cards and presets are then the `cards` and `presets` folders here.

Two sample cards are included: *The Rusty Lantern*, a full RPG with stats, skills, levels and
turn-based fights, and *Quiet Hours*, a slice-of-life card that tracks nothing but friendships.

Your API keys are handed to your operating system's own store for secrets (the Keychain on
macOS, an account-encrypted file on Windows, the desktop's secret service on Linux) and are never
written into this project, a card, a save or the game's saved settings. Where a system has no such
store, they go into a private file outside the game folder that is scrambled, not encrypted.

## Making cards

Press **Card creator** on the game's main menu. From source, run:

```
python3 creator/server.py
```

Either way the creator opens in your browser. It edits the card folders the game reads, so a card
you are working on can be inserted in the game straight away. Every system is optional per card:
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

## Presets

A preset is everything the models are told, for any card: the instructions given to the story
model, the wording of the game's own rules, and the prompt of every helper job. The creator's
**Presets** page edits all of it, marks which parts are yours to change and which the game fills
in, and can show one whole turn exactly as it is sent. A card's own instructions for the narrator
take precedence over the preset.

Presets are shared as `.preset.json` files: **Export** in the creator, **Import** in the creator
or on the game's Preset screen.

## Editing cards with an AI assistant

`.mcp.json` registers the MCP server with Claude Code when you open this folder; approve it once
when asked. For other MCP clients, the command is `python3 creator/mcp_server.py`. The assistant
can then list, read, create and edit cards, add images, import lorebooks and pack cards, and
read, write and remove presets. It is told after every change what the game's own checks make of
the result.

## For developers

```
python3 -m unittest discover tests
```

The game logic in `game/aigame/` is plain Python with no Ren'Py imports, which is what lets it be
tested directly and shared with the creator. The card, preset, action and settings formats are
described in `spec/`. `tests/test_in_step.py` fails when the engine, those format files, the
creator and the MCP server stop agreeing with each other.

Nothing here needs installing beyond Python 3.10 or later and the Ren'Py SDK.

Every model-facing text the game writes itself is registered in `game/aigame/wording.py`, which
is what makes it editable in presets; a test fails if a helper job is added without it.

### Releasing

```
tools/release_check.sh /path/to/renpy-sdk
```

This runs the unit tests, then the game's own self-test (`game/selftest.rpy`: a short game
played against a pretend model, with a save, an export and an import) from source, then builds
the downloads and runs the same self-test inside the built app. Tag only after it passes.

Pushing a tag that starts with `v` makes GitHub build the Windows/Linux and macOS zips and
publish them as a release of that name. A tag with a hyphen, such as `v1.0-beta2`, is published
as a pre-release.

## Licence

SI-Station is free software under the [GNU Affero General Public License, version 3](LICENSE).
You may use, change and share it, including commercially, as long as anything you distribute or
run as a service for others stays under the same licence with its source available.

The licence covers the program. Cards are your own work: a card you make, and the art and writing
in it, are yours to license however you like.

Copyright (C) 2026 nirwannrs
