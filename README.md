# genericMud

An accessible MUD client that talks. It speaks the game through the screen
reader you already run (NVDA, JAWS, VoiceOver — with a system-voice fallback),
plays soundpacks, and gives you keyboard tools for everything: reviewing
output, recalling chat, walking, and building triggers — no scripting needed.
It's a modern, free replacement for VIPMud, and it loads many VIPMud `.set`
and MUSHclient soundpacks as-is.

## Getting it running

**Windows (easiest):** download `genericMud-windows.zip` from the
[Releases page](https://github.com/matalvernaz/genericmud/releases), unzip it
anywhere, and run `genericMud.exe`. Everything it saves (worlds, soundpacks,
logs) stays in a `genericmud-data` folder next to the exe, so the folder is
self-contained and portable. The app checks for new releases itself and offers
to update in place.

**From source** (any platform): see `WINDOWS.md` for the one-click `run.bat`,
or the "For developers" section below.

## Connecting to a MUD

1. Press **Ctrl+N** (or Alt+F, then Connect).
2. Type a name, host, and port — for example Aardwolf: host `aardmud.org`,
   port `4000`. Tick "Save this world" so it's in the list next time.
3. Press Enter.

Type commands in the command box and press Enter to send them. `look`,
`north` (or just `n`), `say hello`, and `help` are good first commands on
almost any MUD. **Up/Down** step through what you've typed before.

The output box above holds everything the MUD said. **Tab** moves between the
output and command boxes; if you start typing while in the output box you
land back in the command box automatically.

## More than one MUD at once

**Ctrl+N** again opens a second world in its own tab. **Ctrl+Tab** and
**Ctrl+Shift+Tab** switch between them. Only the tab you're on speaks; the
others stay quiet but keep playing — triggers still fire and sounds still
play, so you miss nothing.

## Making it talk the way you want

- **Ctrl+F — follow mode.** When you move to a new room, speech cuts straight
  to the new room instead of finishing the old one. Chat and combat still
  queue up. This is the one to try first if the voice always feels behind.
- **Ctrl+I — interrupt mode.** Every new line barges in. For fast fights.
- **View menu → Background silence.** genericMud stays quiet while you're in
  another window (sounds and triggers keep running), and picks up again when
  you come back.
- **Ctrl+M** turns self-voice off entirely so you can read the output box
  with your screen reader's own commands instead.
- **Esc** or **F11** shuts the voice up right now. **Shift+F11** stops every
  playing sound (the panic button for a stuck looping ambience).

If output floods in faster than speech can keep up, genericMud speaks what it
can and says "12 more lines" instead of falling minutes behind — the full
text is always in the output box.

## Reading back what happened

- **Ctrl+1 through Ctrl+9** speak the last nine lines, newest first.
- **Alt+Up/Down** walk the output line by line; **Alt+Left/Right** by word;
  **Alt+Shift+Left/Right** by character; **Alt+Home/End** jump to the oldest
  or newest line.
- **Alt+Shift+Enter** spells the current line out character by character.
- **Alt+T** repeats the last tell; **Alt+C** the last chat line.

**Chat channels** get their own history. When your triggers route lines to
channels (tells, gossip, auction...), **Ctrl+Alt+Left/Right** cycle between
those channels, **Ctrl+Alt+Up/Down** scroll within the one you're on, and
**Ctrl+Alt+1 through 9** read its recent messages — all without touching the
main output.

## Getting around the game

- The **numpad** is a compass: 8 north, 2 south, 4 west, 6 east, the corner
  keys are the diagonals, 5 or 0 look, `.` scans, `-` goes up, `+` goes down.
  If NVDA's desktop layout needs your numpad, turn this off under View.
- Type `.3n2e` to speed-walk three north and two east. Type `..3n2e` to walk
  it one room at a time, stopping if something blocks the way.
- **Alt+B** drops a breadcrumb. Wander wherever; **Alt+R** walks you straight
  back, skipping any detours you took. **Alt+W** says where you are (on MUDs
  that support GMCP). **Alt+S** stops a walk in progress.
- Typing `sh goblin` when you made an alias `sh *` → `shoot %1`? That's in
  the soundpack builder, next section.

## Soundpacks, triggers, and aliases

**Ctrl+B** opens the soundpack builder — the no-scripting way to make the MUD
react. A trigger watches for text and can play a sound (with volume and
stereo position), speak something different, send a command back, hide the
line, interrupt speech, or file the line under a chat channel. You choose how
it matches: "the line contains this text" is the simple default; wildcards
(`*` catches anything, so `* tells you *` captures who and what), whole-line,
and regular expressions are there when you want them. Aliases shorten what
you type (`sh *` sends `shoot %1`), and hotkeys bind a key to a command —
press the key combination you want and it's captured. Everything saves
immediately and works on the very next line from the MUD.

Ready-made packs: **File → Browse Soundpacks Online** pulls from the
community Soundpack Vault, and **File → Set Up a Soundpack** installs one
from a folder, zip, or git URL. VIPMud `.set` packs and MUSHclient packs
load too (MUSHclient ones ask for your trust first, because they contain
code).

## Sharing your setup

**File → Export This World** saves the world you're on — connection details,
all your builder triggers, aliases, hotkeys, channels, and every sound file
they use — as one zip. Send it to a friend; they pick **File → Import a
World** and the whole thing lands in their Connect dialog, sounds included.

## Every keyboard shortcut

Menus: **Alt+F** File, **Alt+R** Rules, **Alt+V** View, **Alt+H** Help.

| Keys | What they do |
| --- | --- |
| Ctrl+N | Connect (new world or saved) |
| Ctrl+D | Disconnect this tab |
| Ctrl+W | Close this tab |
| Ctrl+Tab / Ctrl+Shift+Tab | Next / previous session |
| Ctrl+Q | Exit |
| Enter | Send the command line |
| Up / Down | Command history |
| Ctrl+Enter | Toggle autoretype (empty Enter resends your last command) |
| Ctrl+Space / Ctrl+Shift+Space | Complete the word you're typing from recent output |
| Numpad | Compass walking (View menu toggle) |
| Ctrl+M | Self-voice on/off |
| Ctrl+F | Follow mode (speech interrupts on room movement) |
| Ctrl+I | Interrupt mode (every line barges in) |
| Esc / F11 | Stop speech now |
| Shift+F11 | Stop all sounds (panic) |
| Ctrl+1..9 | Recall the last nine lines |
| Alt+Up / Alt+Down | Review line by line |
| Alt+Left / Alt+Right | Review word by word |
| Alt+Shift+Left / Right | Review character by character |
| Alt+Home / Alt+End | Oldest / newest line |
| Alt+Shift+Enter | Spell the current line |
| Alt+T / Alt+C | Last tell / last chat |
| Ctrl+Alt+Left / Right | Previous / next chat channel |
| Ctrl+Alt+Up / Down | Scroll within the current channel |
| Ctrl+Alt+Shift+Left / Right | Word by word in the channel line |
| Ctrl+Alt+1..9 | Recent messages on the current channel |
| Alt+B / Alt+R | Drop a breadcrumb / retrace to it |
| Alt+W / Alt+S | Where am I / stop walking |
| Ctrl+P | Manage soundpacks |
| Ctrl+B | Soundpack builder |
| Alt+Shift+L | Log this session to a file |
| Alt+Shift+D | Speak the diagnostic log location and summary |

## When something goes wrong

- **No speech at all:** genericMud speaks through NVDA or JAWS if one is
  running, and falls back to the Windows voice if not. Check **Ctrl+M**
  wasn't toggled off, and check View → Background silence isn't on while
  you're testing from another window.
- **A soundpack is silent:** press **Alt+Shift+D** — it speaks where the
  diagnostic file is and a one-line summary that usually names the problem
  (pack failed to load, no triggers registered, sound file missing).
- **A looping sound won't stop:** **Shift+F11**.
- **Logs and saved data** live in `genericmud-data` next to the exe (or
  `~/.genericmud` when running from source).
- Found a bug? Open an issue or send your newest `crash-*.log` and
  `diagnostic-*.log` from the logs folder.

## For developers

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Native Python asyncio engine (transport, telnet/MCCP/GMCP/MSDP/MSSP/MSP,
ANSI, triggers/aliases/timers, Lua + VIPMud + MUSHclient dialects, voice
router) with a wxPython native UI, pygame audio, and an alternate web UI
(`--web`) over a localhost WebSocket. The engine is headless-testable; the
whole suite runs without a display, socket, or screen reader. Runtime deps:
`lupa` (Lua) and `regex` (ReDoS-safe matching). Extras: `.[gui]` webview
shell, `.[voice]` native voice backends, `.[audio]` pygame.

Windows packaging and running from source: `WINDOWS.md`.
