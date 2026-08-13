# genericMud

An accessible MUD client that talks. It speaks the game through the screen
reader you already run (NVDA, JAWS, VoiceOver — with a system-voice fallback),
plays soundpacks, and gives you keyboard tools for everything: reviewing
output, recalling chat, walking, mapping, and building triggers — no scripting
needed. It's free, and it loads many existing VIPMud `.set` and MUSHclient
soundpacks as-is, so a pack you already use very likely just works.

## Start here

Four things get you playing:

1. **Ctrl+N**, then type a name, host, and port, and press Enter.
2. Type commands in the command box and press Enter. **Up** and **Down** are
   your history.
3. **Tab** moves between the output box and the command box. Start typing in the
   output box and you land back in the command box automatically.
4. **Esc** shuts the voice up. **Shift+F11** stops every sound.

Everything below is depth you can come back for. The full key list is near the
end, and there's a plain-language [automation and Lua scripting
guide](docs/scripting.md) for the deeper end of triggers and scripts.

## Getting it running

Download the build for your platform from the
[Releases page](https://github.com/matalvernaz/genericmud/releases). It reads
your screen reader on every platform, and self-voices live output through the
platform's own speech.

**Windows:** unzip `genericMud-windows.zip` anywhere and run `genericMud.exe`.
Everything it saves (worlds, soundpacks, maps, logs) stays in a
`genericmud-data` folder next to the exe, so it's self-contained and portable.
Self-voice reads through NVDA or JAWS if one is running, or the Windows voice
otherwise.

**Mac:** unzip `genericMud-macos.zip` and drag `genericMud.app` to Applications.
Self-voice speaks through VoiceOver when it's running and the built-in macOS
speech otherwise. Saved data lives in `~/Library/Application Support/genericMud`.

**Linux:** untar `genericMud-linux.tar.gz` and run `genericMud` from the folder.
Self-voice speaks through Orca when it's running, and otherwise needs
`speech-dispatcher` installed (the same speech engine Orca uses —
`sudo apt install speech-dispatcher`).

**From source** (any platform): install [uv](https://docs.astral.sh/uv/), then
`uv run --extra gui --extra voice --extra audio genericmud` — it fetches Python
and the locked dependencies on first run. See "For developers" below.

## Connecting to a MUD

1. Press **Ctrl+N** (or Alt+F, then New World).
2. Fill in a name, host, and port — for example Aardwolf: host `aardmud.org`,
   port `4000`. There's a **Use TLS** box for MUDs that want an encrypted
   connection, and a **Sounds folder** box if you already keep that MUD's sound
   files somewhere. "Save this world" is checked by default.
3. Press Enter.

Next time, press **Ctrl+O** to pick the world from your saved list; that dialog
can also edit a saved world or start a new one. **Ctrl+D** disconnects and
**Ctrl+W** closes the tab. If the connection drops on its own, genericMud
reconnects for you, backing off if the MUD stays down; quitting from inside the
game doesn't trigger that.

`look`, `north` (or just `n`), `say hello`, and `help` are good first commands
on almost any MUD. A few conveniences in the command box:

- **Up / Down** step through what you've typed before.
- A **semicolon** splits one line into several commands, so `n;n;look` sends
  three.
- **Ctrl+Enter** turns on autoretype: pressing Enter on an empty line resends
  your last command, which saves a lot of typing in a fight.
- **Ctrl+Space** completes the word you're typing from words that recently came
  out of the MUD — useful for long names you'd rather not spell. **Ctrl+Shift+Space**
  cycles backwards through the matches.

## More than one MUD at once

**Ctrl+N** opens another new world and **Ctrl+O** another saved one, each in its
own tab. **Ctrl+Tab** and **Ctrl+Shift+Tab** switch between them. Only the tab
you're on speaks; the others stay quiet but keep running — triggers still fire
and sounds still play, so you miss nothing. To reach across, `/to` sends a
command to another tab by its world name: `/to alter aeon look`.

## Making it talk the way you want

- **Ctrl+Shift+F — follow mode.** When you move to a new room, speech cuts
  straight to the new room instead of finishing the old one. Chat and combat
  still queue up. This is the one to try first if the voice always feels behind.
- **Ctrl+I — interrupt mode.** Every new line barges in. For fast fights.
- **View menu → Background silence.** genericMud stays quiet while you're in
  another window (sounds and triggers keep running), and picks up again when you
  come back.
- **Ctrl+M** turns self-voice off entirely so you can read the output box with
  your screen reader's own commands instead.
- **Esc** or **F11** shuts the voice up right now. **Shift+F11** stops every
  playing sound — the panic button for a stuck looping ambience.

If output floods in faster than speech can keep up, genericMud speaks what it
can and says "12 more lines" instead of falling minutes behind. The full text is
always in the output box.

## Reading back what happened

- **Ctrl+1 through Ctrl+9** speak the last nine lines, newest first.
- **Alt+Up/Down** walk the output line by line; **Alt+Left/Right** by word;
  **Alt+Shift+Left/Right** by character; **Alt+Home/End** jump to the oldest or
  newest line.
- **Alt+Shift+Enter** spells the current line out character by character.
- **Alt+T** repeats the last tell; **Alt+C** the last chat line.
- **Ctrl+F** searches the output, and **F3** / **Shift+F3** find the next and
  previous match. These work while the output box has focus.

**Chat channels** get their own history. When your triggers route lines to
channels (tells, gossip, auction...), **Ctrl+Alt+Left/Right** cycle between
those channels, **Ctrl+Alt+Up/Down** scroll within the one you're on, and
**Ctrl+Alt+1 through 9** read its recent messages — all without touching the
main output.

**Alt+Shift+L** starts and stops writing everything to a log file, if you want a
transcript to read later. While the MUD is hiding your typing — which is how
they ask for a password — the log records `***` instead of what you typed.

## Getting around the game

- The **numpad** is a compass: 8 north, 2 south, 4 west, 6 east, the corner keys
  are the diagonals, 5 or 0 look, `.` scans, `-` goes up, `+` goes down. If
  NVDA's desktop layout needs your numpad, turn this off under View.
- Type `.3n2e` to speed-walk three north and two east. Type `..3n2e` to walk it
  one room at a time, stopping if something blocks the way.
- **Alt+B** drops a breadcrumb. Wander wherever; **Alt+R** walks you straight
  back, skipping any detours you took. **Alt+W** says where you are, on MUDs that
  share your location with the client. **Alt+S** stops a walk in progress.
- **Alt+M** and `/goto` map and navigate, on MUDs that report rooms — see below.

The rest of this section is the detail: how to write a route, how the two kinds
of walk differ, what the breadcrumb trail can and can't take you back through,
and what mapping adds on top. Other clients call some of this fastwalk.

### Writing a route

A route is a run of directions, each with an optional count in front of it. `3n`
is three north; `n` on its own is once. The directions are `n`, `s`, `e`, `w`,
`ne`, `nw`, `se`, `sw`, `u` for up and `d` for down. Case doesn't matter and
there are no spaces or separators, so `.2se4n` is a valid route.

Anything else in the run means it isn't a route at all, and genericMud sends the
line to the MUD as an ordinary command instead. `.3north` and `.n,n,e` go to the
game as text, and so does a zero count like `.3n0e` — a leg you count zero times
is a typo, not one you meant to skip. Routes are capped at 1000 steps, so a
slipped keypress like `.999999999n` is refused rather than flooding the MUD.

A route understands compass directions only. Exits your MUD spells out in words
— `enter portal`, `out`, `climb rope` — aren't part of one. Type those yourself.

### `.` walks it now, `..` walks it carefully

`.3n2e` sends all five moves at once, as fast as the connection carries them.
It's the quick one, and it's the right choice on a route you know is clear.

`..3n2e` sends one move, waits until you've actually arrived, and then sends the
next. On a MUD that tells the client which room you're in (over GMCP or MSDP)
that wait is exact. On a MUD that doesn't, it waits about half a second per step
and carries on, which is slower but still lands you in the right place on a
laggy link.

Either way, if the MUD answers a step with something like "You can't go that
way", "There is no exit" or "The door is closed", the walk gives up there and
says "path blocked, 4 steps abandoned" rather than firing the rest of the route
into a wall. It says "arrived" when it finishes. **Alt+S** stops it early, and
starting another walk cancels one already in progress.

**Alt+S** only has something to stop during a `..` walk. A `.` route and a
retrace are already on their way to the MUD by the time you could press it.

### The breadcrumb trail

genericMud keeps a record of the compass moves you make — pressed on the numpad,
typed short as `n` or `se`, or sent by either kind of walk. That record is the
trail, and **Alt+R** turns it into the way home: the same steps in reverse, each
one flipped to its opposite.

**Alt+B** drops a breadcrumb, which means "start measuring from here". It
forgets the trail so far and begins a new one in your current room, so press it
in the spot you want to be able to come back to.

Retracing leaves your detours out. Go north, then east and straight back west,
and the east and west cancel each other out, so **Alt+R** just sends south. That
folds as deep as it needs to: three rooms up a dead end and back again adds
nothing to the way home.

**Alt+R** sends the whole way back in one burst, the way `.` does, and then
forgets the trail on the assumption you made it. It won't stop partway if a door
has closed behind you, so on a route that might have changed, listen to the
output as it goes and drop a fresh breadcrumb once you're somewhere known.

Two things don't make it into the trail, because genericMud never sees a
direction for them. One is any move that isn't a compass direction: `enter
portal`, a teleport, a mount that carries you off, or an exit your MUD names in
words. The other is a direction typed out in full — `north` sends and works, but
only the short `n` records a step. After either, the way back isn't in the trail,
so drop a new breadcrumb with **Alt+B**.

### Where am I

**Alt+W** speaks the room name, the area, the exits, and how many steps you are
from your breadcrumb. The room details come from the MUD over GMCP or MSDP, so on
a MUD that shares nothing you'll hear "no location info" — the step count still
works, because that's genericMud's own count of your moves.

### Mapping, on MUDs that report rooms

Some MUDs tell the client which room you're in every time you move, including
where each of its exits leads. On those, genericMud builds a map as you explore,
and you get things the breadcrumb trail can't do — routes between places you
never walked in that order, and a way to tell explored from unexplored.

**Alt+M** reads out the room you're in: its name, its area, and each exit with
where it goes. Exits leading somewhere you've never stood are called
"unexplored", which turns working through a new area into a checklist instead of
guesswork.

**`/goto`** followed by a room name walks you there — `/goto bank`. It takes the
shortest way the map knows, one room at a time, so it stops if something blocks
the way. It also stops if anything moves you off the route, such as a teleport
or a trapdoor, instead of carrying on from the wrong place. You can only go to
somewhere you've actually stood: a room you've only ever seen named as an exit
has no name for you to ask for yet.

**`/label`** followed by a name renames the room you're in. `/label smithy` and
then `/goto smithy` works from then on, whatever the MUD calls it.

**`/map`** says how many rooms you've mapped and how many unexplored exits are
left.

Maps are saved per world, so yours is still there next time you connect.
Nothing is ever guessed: only rooms the MUD identifies itself go on the map,
which is exactly why a MUD that shares nothing gets no map instead of an
unreliable one that would walk you into a wall. If **Alt+M** says the room isn't
on the map, either that MUD doesn't report rooms, or you're somewhere it keeps
off its own map — some clan halls and quest rooms are deliberately unmapped.

## Automation: triggers, aliases, hotkeys, channels, and scripts

**Ctrl+B** opens one Automation Manager for the current world. Choose
**Triggers**, **Aliases**, **Hotkeys**, **Channels**, or **Scripts** from the
**Show** box. There is no separate simple or advanced builder.

A trigger reacts to text from the MUD. It can play a sound, speak something
shorter, send commands, hide the matched line, interrupt speech, or route the
line to a chat channel. An alias replaces a shortcut you type: `sh *` can send
`shoot ${1}`. A hotkey runs commands when you press a chosen key. These editors
use ordinary fields; no code is required.

Each trigger, alias, or hotkey can send one command or a sequence. Put one
command on each line. The whole sequence is filled in before its first command
is sent. Command fields understand matched text (`${1}` or the older `%1`), a
value saved by a script (`${script:target}`), and live data sent by the MUD, such
as MSDP `${mud:HEALTH}` or GMCP `${mud:Char.Vitals.hp}`. If a value isn't
available, none of that sequence is sent and genericMud speaks the problem once.

Use **Duplicate** to start from an existing item. **Disable** keeps a trigger,
alias, hotkey, or channel saved without letting it run. Changes work on the next
matching line.

Choose **Scripts** in the same manager when the automation needs decisions,
timers, reusable functions, or other Lua code. Scripts are sandboxed, load
alphabetically, and can be saved and reloaded without reconnecting. For example:

```lua
mud.alias("combo *", function(line, captures)
    mud.set_var("target", captures[1])
    mud.command({"stand", "kill ${script:target}", "consider ${1}"})
end)
```

Read the [step-by-step automation and scripting guide](docs/scripting.md) for
plain-language instructions, copyable examples, and the full Lua API. The same
shorter guide is available offline under **Automation → Automation help**.

### Quick aliases and triggers, typed

For something you only need right now, you can skip the dialog and type it:

- `/alias sh = shoot goblin` — typing `sh` sends `shoot goblin`. Your shortcut
  has to be the whole line, exactly.
- `/trigger you are hungry = eat bread` — when that text turns up anywhere in a
  line from the MUD, it sends `eat bread`. Case doesn't matter.
- `/aliases` and `/triggers` read back what you've made this session.
- `/unalias sh` and `/untrigger you are hungry` remove one.

These last for the current session only. Anything worth keeping belongs in the
Automation Manager, which saves it per world.

## Soundpacks

**Soundpacks → Browse soundpacks online** (or **Ctrl+Shift+B**) pulls from the
community Soundpack Vault; it hides packs written for clients genericMud can't
load, with a checkbox to show them anyway. **Set up a soundpack from a folder**
installs one from a folder, a zip, or a download link. **Manage installed
soundpacks** (**Ctrl+P**) turns packs on and off per world, checks a pack for
conflicts with your other packs, updates one from where it came from, and
uninstalls.

Native Lua packs, VIPMud `.set` packs, and MUSHclient `.xml`/`.mcl` packs all
load. A pack has to be both enabled for the world and trusted before it runs on
connect — MUSHclient packs contain real code, so genericMud asks before running
one rather than assuming. Sounds a pack can't find are reported instead of
failing silently, and a pack that fails to load doesn't take the session with
it.

If the MUD drives the sounds itself (many do, over MSP), there's nothing to
install: genericMud plays what the MUD asks for.

## Sharing your setup

**File → Export This World** saves the world you're on — connection details, all
your triggers, aliases, hotkeys, channels, every sound file they use, and its
automation scripts — as one zip. Send it to a friend; they pick **File → Import
a World** and the whole thing lands in their Connect dialog, sounds and scripts
included.

## Keeping it up to date

**Help → Check for Updates** asks whether there's a newer release and reads out
what changed. On Windows it can install the update and restart itself; on Mac
and Linux it opens the release page for you to download the new build.
genericMud also checks quietly at startup and only speaks up if there's
something to offer, so it never delays a launch.

## Every keyboard shortcut

Menus: **Alt+F** File, **Alt+P** Soundpacks, **Alt+A** Automation, **Alt+V**
View, **Alt+H** Help.

| Keys | What they do |
| --- | --- |
| Ctrl+N | Create and connect to a new world |
| Ctrl+O | Connect to a saved world |
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
| Ctrl+Shift+F | Follow mode (speech interrupts on room movement) |
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
| Ctrl+F | Find in the output (while the output has focus) |
| F3 / Shift+F3 | Find next / previous |
| Ctrl+Alt+Left / Right | Previous / next chat channel |
| Ctrl+Alt+Up / Down | Scroll within the current channel |
| Ctrl+Alt+Shift+Left / Right | Word by word in the channel line |
| Ctrl+Alt+1..9 | Recent messages on the current channel |
| Alt+B / Alt+R | Drop a breadcrumb / retrace to it |
| Alt+W / Alt+S | Where am I / stop walking |
| Alt+M | Read the mapped room and where its exits lead |
| Ctrl+P | Manage soundpacks |
| Ctrl+Shift+B | Browse soundpacks online |
| Ctrl+B | Open the Automation Manager |
| Ctrl+1..5 in Automation Manager | Show triggers / aliases / hotkeys / channels / scripts |
| Ctrl+N in Automation Manager | Create an item in the category being shown |
| Enter / Delete in an automation list | Edit / delete the selected item |
| F2 in the Scripts list | Rename the selected script |
| Alt+Shift+L | Log this session to a file |
| Alt+Shift+D | Speak the diagnostic log location and summary |

Typed commands: `.3n2e` and `..3n2e` walk a route, `n;n;look` sends several
commands, and `/goto`, `/label`, `/map`, `/alias`, `/trigger`, `/aliases`,
`/triggers`, `/unalias`, `/untrigger` and `/to` are described in the sections
above.

## When something goes wrong

- **No speech at all:** genericMud speaks through NVDA or JAWS if one is
  running, and falls back to the system voice if not. Check **Ctrl+M** wasn't
  toggled off, and check View → Background silence isn't on while you're testing
  from another window.
- **A soundpack is silent:** press **Alt+Shift+D** — it speaks where the
  diagnostic file is and a one-line summary that usually names the problem (pack
  failed to load, no triggers registered, sound file missing).
- **A looping sound won't stop:** **Shift+F11**.
- **Alt+M says the room isn't on the map:** that MUD doesn't report rooms to the
  client, or the room you're in is one it keeps off its own map. Mapping,
  `/goto` and Alt+W all depend on the MUD sharing your location.
- **Logs and saved data** live in `genericmud-data` next to the exe (or
  `~/.genericmud` when running from source).
- Found a bug? Open an issue or send your newest `crash-*.log` and
  `diagnostic-*.log` from the logs folder.

## For developers

[uv](https://docs.astral.sh/uv/) manages the environment — it installs Python
3.12 itself (`.python-version`) and every dependency version is pinned in the
committed `uv.lock`, so CI and your machine resolve identically.

```sh
uv sync --all-extras   # the suite needs `gui` for websockets, so not plain `uv sync`
uv run pytest -q
uv run ruff check .
```

Native Python asyncio engine (transport, telnet/MCCP/GMCP/MSDP/MSSP/MSP, ANSI,
triggers/aliases/timers, room mapping, Lua + VIPMud + MUSHclient dialects, voice
router) with a wxPython native UI, pygame audio, and an alternate web UI
(`--web`) over a localhost WebSocket. The engine is headless-testable; the whole
suite runs without a display, socket, or screen reader. Runtime deps: `lupa`
(Lua) and `regex` (ReDoS-safe matching). Extras: `.[gui]` webview shell,
`.[voice]` native voice backends, `.[audio]` pygame; the test/lint tools are the
`dev` dependency group, which `uv sync` installs by default.

The same wxPython UI runs on all three platforms. Self-voice goes through
[prism](https://github.com/ethindp/prism) (`prismatoid` on PyPI), one API over
every screen reader and system TTS: it speaks in the user's own NVDA, JAWS,
VoiceOver or Orca voice when one is running, and drops to SAPI/OneCore, AVSpeech
or speech-dispatcher when none is. If prism itself is unavailable the client
falls back to SAPI on Windows, `say` on macOS, and `spd-say` on Linux. Build a
frozen app for the current platform with `uv run python tools/build_app.py` —
`.exe` onedir on Windows, `genericMud.app` on macOS, an onedir tarball on Linux.
CI builds all three on tags (`build-windows.yml`, `build-macos.yml`,
`build-linux.yml`).

Windows packaging and running from source: `WINDOWS.md`.
