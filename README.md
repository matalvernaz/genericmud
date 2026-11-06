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
   connection, an **Encoding** choice for MUDs that don't send UTF-8, and a
   **Sounds folder** box if you already keep that MUD's sound files somewhere.
   Leave it blank when a soundpack brings its own sounds.
3. Press Enter.

**When I press OK** offers three things, and says all three as you arrow
through them:

| Choice | What OK does |
|---|---|
| Save and connect (the default) | Adds the world to your list and opens a tab on it |
| Save, don't connect | Adds it to your list and opens no tab; **Ctrl+O** connects when you want |
| Connect, don't save | Opens a tab on a world you are only visiting: nothing is kept, the login you typed included |

Saving without connecting is the way in when the MUD is down, or when you are
setting worlds up before playing. There is deliberately no choice that does
neither, so the OK button always has something to do.

Leave **Encoding** on Automatic unless accented or non-Latin letters come out
wrong. Automatic reads UTF-8 and switches to Windows Western European the moment
a MUD sends anything else, which suits most older European MUDs. A MUD that uses
another encoding needs it chosen: Russian MUDs usually offer Windows-1251 or
KOI8-R, Chinese ones GBK or Big5. What you type is sent in the same encoding.
Choosing UTF-8 itself helps too, on a UTF-8 MUD that sometimes sends one stray
byte: Automatic would take that byte as a sign of an older encoding and misread
the rest of the session. To change it for a saved world, press **Ctrl+O**,
choose the world, and press **Edit**; it applies the next time that tab connects.
A tab takes its host, port, TLS switch and encoding from the saved world every
time it connects — including a reconnect the client makes by itself after a drop —
so editing them and pressing Ctrl+D uses the new ones.
They are read together on purpose: a tab can never end up reaching a new host
with the old encoding. The sounds folder is the exception — packs load when a tab
starts, so a new one needs a new tab.

A world's details also hold its **command separator**, **logging**, **login**
and **images address** settings, described below. Changes to those apply
straight away, even to an open tab.

**`/secure`** takes up the encrypted port a game advertised when you connected. Games often
keep a secure port that they never mention: they announce it over MSSP, one of the status
protocols the client already reads, and the client says so in passing at connect — "this game
offers an encrypted connection on port 9501; /secure switches this world to it". Typing
`/secure` sets the world's port and TLS switch, saves the world if it is one you saved, and
reconnects. Nothing is switched on a game's say-so alone; the offer is only made once, and only
when the connection you are on isn't already encrypted.

**Understand Pueblo HTML** is off unless you switch it on, and unlike the other
settings that is deliberate: turning it on makes genericMud introduce itself as a
Pueblo client when it connects, and a MUD that does not speak Pueblo reads that
line as a character name at its login screen. Switch it on for a world you know
wants it. genericMud then reads that world's HTML as the words it contains --
tags are dropped rather than spoken, `&lt;` and `&quot;` become the characters
they stand for, and a line break is where the MUD put one -- so a Pueblo MUD
reads as prose instead of a wall of markup. Commands the world offers as links
(`<a xch_cmd="north">north</a>`) become the same followable links an MXP world's
are, so the link keys work there too. Colour and pictures carry nothing a voice
can use and are left out.

## Logging in automatically

A world's details (Ctrl+O, choose the world, Edit) take a **username** and a
**password**, which are kept in your computer's own credential store — the
Windows Credential Manager, the macOS Keychain, or the Secret Service on Linux.
They are encrypted there with your own login, so there is no master password to
type and nothing readable sitting among genericMud's files. The field starts
masked; **Reveal password** shows it, and it is off every time the dialog opens.
On a machine with no usable credential store, genericMud falls back to the
plaintext `credentials.json` and says so out loud when it starts.

**Connect** chooses how the login is sent, and a world starts on No auto-connect:
with the boxes empty there is nothing to send, and no login method should be
applied that you didn't choose. Pick the one your MUD expects.

| Choice | What it sends |
|---|---|
| No auto-connect | Nothing. You log in yourself |
| Answer the MUD's prompts | The name when asked, then the password once the MUD asks for it. Works with almost everything, and never sends the password before the username |
| MUSH/MOO | The single command `connect name password` |
| Diku | The name, then the password, each on its own line |

**Sent after connecting** takes commands, one per line, typed as soon as the
connection is up — or, when a login is configured, the moment that login
finishes. A reconnect types them again. For example, `look` and `score` on two
lines get you oriented each time you connect.

Next time, press **Ctrl+O** to pick the world from your saved list; that dialog
can also edit a saved world, delete one, or start a new one. Editing one
includes its name, so a world can be renamed without editing any file — unless a
tab is connected to it, because a connected tab is using that name: it is the
log file being written, the `/to` address and the tab's own label. Disconnect
the tab (Ctrl+D) and the name is yours to change; a tab that is only
disconnected takes the new name and writes its next log under it. The world's
own data follows the name, so a rename keeps its room map, its Automation
Manager rules and its saved pack variables. The dialog says so when a name can't
be changed. **Delete** asks first, and says exactly what it takes: the saved
world and its login, and nothing else. Its logs, its sound folder, its map, its
saved variables and any automation rules of its own all stay, so adding the
world back by the same name finds them again. The button is **Delete...**, and
its key is in its name where a screen reader reads it: **Alt+D**. The **Delete**
key does the same while the saved world list has the focus. **Ctrl+D**
disconnects and **Ctrl+W** closes the tab. If the MUD is unavailable at startup
or the connection drops on its own, genericMud keeps trying every five seconds
until it returns. **File → Connection settings...** turns Auto reconnect on or
off and sets the interval (1–300 seconds) for all worlds; both settings survive
a restart. Turning it off leaves manual Reconnect available. Quitting from
inside the game doesn't trigger recovery, and **Ctrl+D** while it's still trying
stops that session without changing the global setting.

**Ctrl+D** takes a disconnected tab back to the MUD as well — the File menu
entry changes to **Reconnect** to say so. Reconnecting reuses the tab, so the
scrollback you already have stays put. Typing a command for the MUD into a
disconnected tab asks whether to reconnect rather than dropping the command
silently, and what you typed stays in the box, ready to send once you're back.
While genericMud is already trying to reconnect by itself, it says "Still
connecting" instead of asking.
genericMud's own slash commands, such as `/to` and `/aliases`, still run without
asking.

Up and Down walk your command history only when the box is empty, or when it holds
a command you have already sent and the caret is at the start or the end of it.
Anything else is a draft you are still writing: the arrows move the caret and leave
the text alone, because losing typed-but-unsent input is silent data loss when you
cannot glance at the box. Arrowing past the newest entry empties the box, and
**Ctrl+A** then Backspace clears a draft when you want it gone.

In the native window the box holds several lines. Enter sends what is there,
and **Shift+Enter** starts another line for the next command; a pasted block
needs neither, since its lines are already separate. Each line goes out as its
own command, in order -- a way to send several commands at once to a world that
has stacking off, or to paste a list you have been keeping somewhere else. One
thing to know: a MUD reads a line break as Enter itself, so a break can
separate two commands but can never be part of one. A long `say` wraps on
screen by itself. A block of slash commands only needs no connection, exactly
as one line of them doesn't. The web UI's box is still a single line, so none
of this applies there yet.

`look`, `north` (or just `n`), `say hello`, and `help` are good first commands
on almost any MUD. A few conveniences in the command box:

- **Up / Down** step through what you've typed before.
- A **semicolon** splits one line into several commands, so `n;n;look` sends
  three. Type it twice to send a real one: `say hi;; bye` sends `say hi; bye`.
  Each world can use a different separator, or none, in its details. The
  separator works the same in your own aliases, hotkeys and triggers;
  soundpacks aren't affected.
- **Ctrl+Enter** turns on autoretype: pressing Enter on an empty line resends
  your last command, which saves a lot of typing in a fight.
- **Ctrl+Space** completes the word you're typing from words that recently came
  out of the MUD — useful for long names you'd rather not spell. **Ctrl+Shift+Space**
  cycles backwards through the matches.

### Editable command buffer (native window)

**View → Command input mode → Editable command buffer (VIPMud-style)** selects
an optional editor for newly opened sessions. **Standard** remains the default
with the behavior described above. The choice is saved globally; already open
sessions keep their mode, text, and selection until closed. The web UI keeps its
existing input behavior.

The editable buffer holds old commands and several unsent drafts in their original
places. Up and Down move through those lines. Down on the final unfinished line
adds an empty tail without sending the draft. Tab back from Output, or Ctrl+Enter,
returns to the existing tail; a final draft remains there. Typing from Output
also goes to that tail.

Enter executes the logical line at the start of the selection and leaves an empty
tail, copying an older line to the bottom. Shift+Enter executes the current line,
or only the selected text, without changing the document or selection. An explicit
selection spanning several lines sends its nonblank lines as separate commands.
Adjacent identical completed lines are reduced to one, including manually typed
repetitions, once there are three line separators; very short buffers keep duplicates.

Escape inserts a blank line above the current row; in the first row it splits at
the caret. Ordinary Backspace at the start of a row preserves its separator, while
selected text and Delete remain ordinary native editing. Ctrl+R completes the
current prefix from earlier editable rows, including unsent drafts; repeated
presses cycle backwards. Explicit soundpack/user bindings on Ctrl+R, Ctrl+Enter,
or Escape still win. Ctrl+Enter does not toggle autoretype in this mode; autoretype
keeps its saved setting and can be toggled in Standard or by a custom key binding.

On macOS, use **Command+R** and **Command+Enter** for these two shortcuts;
physical Control+Enter opens Cocoa's native context menu.
Standard keeps its existing wx keyboard mapping.

Passwords use a separate temporary field and never enter the document. Closing a
session discards its in-memory buffer. There is no automatic pruning of old drafts;
long sessions may need manual deletion. Lines scroll horizontally instead of wrapping.
This first version supports the default Line Entry Mode; its Off setting, VIPMud
script-brace assembly, and exact mouse/IME behavior remain outside the compatibility
claim. The original VIPMud behavior was corroborated by a user's keyboard/NVDA
observations. macOS OS-delivered keyboard tests passed, but genericMud's new mode
still needs Windows/NVDA, GTK/Orca, VoiceOver and hardware keyboard/IME validation.
See [command input details](docs/command-entry.md).

## More than one MUD at once

**Ctrl+N** opens another new world and **Ctrl+O** another saved one, each in its
own tab. **Ctrl+Tab** and **Ctrl+Shift+Tab** switch between them and say which
world you landed in; **Alt+1** through **Alt+9** (and **Alt+0** for the tenth) go
straight to a tab, counting in the order you opened them, unless the world's
soundpack or your own hotkeys use those keys. On a Mac they're left free, since
Option and a number types characters there. Out of the box only the tab you're
on speaks; the others stay quiet but keep running — triggers still fire and
sounds still play, so you miss nothing. **View → Silence other worlds** lets them
all speak. To reach across, `/to` sends a command to another tab by its world
name: `/to alter aeon look`.

The window title can name the world you're in — **View → World name in the window
title** — so Alt+Tab and the taskbar read as `genericMud - [Shards of Valdrenne]`
rather than just `genericMud`. It's off until you ask for it.

## Making it talk the way you want

- **Ctrl+Shift+F — follow mode.** When you move to a new room, speech cuts
  straight to the new room instead of finishing the old one. Chat and combat
  still queue up. This is the one to try first if the voice always feels behind.
- **Ctrl+I — interrupt mode.** Every new line barges in. For fast fights.
- **View menu → Background silence.** genericMud stays quiet while you're in
  another window (sounds and triggers keep running), and picks up again when you
  come back.
- **View menu → Silence other worlds.** On by default: only the world you're
  looking at speaks. Turn it off and every open world speaks, so with nine worlds
  open you hear all nine instead of one — each of them still respects background
  silence and Ctrl+M. They share one voice, though: nothing says which world a
  line came from, and with Ctrl+I on, a busy world cuts the others off.
- **Ctrl+M** turns self-voice off entirely so you can read the output box with
  your screen reader's own commands instead.
- **Esc** or **F11** shuts the voice up right now. **Shift+F11** stops every
  playing sound — the panic button for a stuck looping ambience.

If output floods in faster than speech can keep up, genericMud speaks what it
can and says "12 more lines" instead of falling minutes behind. The full text is
always in the output box.

## Reading back what happened

- **Ctrl+1 through Ctrl+9** speak the last nine lines, newest first, and
  **Ctrl+0** speaks the tenth.
- **Alt+Up/Down** walk the output line by line; **Alt+Left/Right** by word;
  **Alt+Shift+Left/Right** by character; **Alt+Home/End** jump to the oldest or
  newest line.
- **Alt+Shift+Enter** spells the current line out character by character.
- **Ctrl+Shift+C** copies from the line you're on, or the newest line if you
  haven't started reviewing. A small box opens with the whole line selected, so
  Enter copies all of it; to copy a name or a number, select just that part
  first with Shift and the arrow keys. Focus goes back where it was afterwards.
- **Alt+Enter** follows the link on the line you're reading. For MUDs that send
  MXP links (an exit or an item that does something when clicked), this is the
  keyboard form of clicking it: the command is sent as if you'd typed it. When a
  line carries several links, **Alt+Left/Right** into the word you want first —
  the cursor's word picks the link — and if the cursor isn't inside one, the
  links are read out rather than guessed. While the output box has focus, plain
  **Enter** does the same thing.
- **Alt+PageDown** jumps on to the next link in the output and **Alt+PageUp**
  back to the previous one, skipping lines without any. You hear the link's
  words and the line it's on, the cursor lands on the link so Alt+Enter follows
  it, and at either end you hear "no more links". Starting outside review,
  Alt+PageUp finds the newest link first. In a world whose soundpack or hotkeys
  use these keys (Erion's history browser does), they stay the soundpack's.
- A MUD can give a link a caption saying what it does, the words a mouse user
  sees under the pointer. When the review cursor arrives on such a link you hear
  the word, then the caption, such as "link: Click to drink"; it isn't repeated
  while you move through the same link's words. Following the link says the
  caption again with the command, before the command goes.
- Some links start a command for you to finish instead of sending one, such as a
  name that offers `tell Zugg ` for you to type the rest of the message. Following
  one puts that text in the command box, says so, and sends nothing until you
  press Enter. What replaces the box's contents is only ever one plain command: a
  link that would type a genericMud command such as `/alias`, or several commands
  joined by your separator, is refused and says why.
- A link can also offer a menu of commands, such as buying or examining an item in
  a shop. When a line's links are read out, such a link has "menu of 3" after it,
  and following it asks which one you meant instead of sending anything: a
  numbered list opens,
  where a number key picks a row straight away when there are nine or fewer,
  arrows and Enter work as usual, and Escape sends nothing. In the web UI the
  choices appear as buttons along the bottom, with a Cancel button, and Escape
  works there too.
- In the **web UI** those links are real links in the page as well, so they can be
  focused and activated like any other link, in addition to the keys above. A link
  to a web page opens in a new tab.
- **Alt+T** repeats the last tell; **Alt+C** the last chat line.
- **Ctrl+F** searches the output, and **F3** / **Shift+F3** find the next and
  previous match. These work while the output box has focus, and each search
  starts from the line the caret is on, so arrowing back through the output and
  searching again carries on from where you are rather than from the last match.

**Chat channels** get their own history. When your triggers route lines to
channels (tells, gossip, auction...), **Ctrl+Alt+Left/Right** cycle between
those channels, **Ctrl+Alt+Up/Down** scroll within the one you're on, and
**Ctrl+Alt+1 through 9** read its recent messages, with **Ctrl+Alt+0** for the
tenth — all without touching the main output.

**Alt+Shift+L** starts and stops writing everything to a log file, if you want a
transcript to read later. In a world's details you can have it log automatically
whenever it connects, choose a new file each session, one file per day or one
file for the world, and leave out what you type. Every log adds to its file and
starts with the date and time. If you stop a log with Alt+Shift+L, it stays
stopped until you close the tab. While the MUD is hiding your typing — which is
how they ask for a password — the log records `***` instead of what you typed.

Logs are kept in two folders, so that reading one back and reporting a problem
never mean sorting through the other:

| Folder | Holds |
|---|---|
| `logs/worlds` | session logs — what the MUD said, and what you typed if the world logs that |
| `logs/diagnostics` | the per-run diagnostic trace and any `crash-*.log` files |

**`/logs`** says both folder paths and how many files are in each. Logs written
by an older version sat together in `logs` itself; the first time this version
starts it moves them into the right folder, and leaves anything it doesn't
recognise exactly where it is.

The same world details also have **Echo input in the output**, off by default,
which puts each command you send into the output as well — buffered like any
other line, so the review keys can read back something you typed that has
scrolled away. It never speaks them, and it shows nothing while the MUD is
hiding what you type. It's separate from the logging one on purpose: wanting
your commands on screen and wanting them in a file are two different things.

## Getting around the game

- The **numpad** is a compass: 8 north, 2 south, 4 west, 6 east, the corner keys
  are the diagonals, 5 or 0 look, `.` scans, `-` goes up, `+` goes down. If
  NVDA's desktop layout needs your numpad, turn this off under View.
  With **Numpad compass walking** on, custom numpad hotkeys are inactive.
  Turn it off under View to use keys such as `numpad5` or `numpad+` in Automation
  hotkeys (Ctrl+B).
  Your screen reader or operating system may intercept physical numpad keys before
  genericMud receives them.
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
typed as `n` or `north`, or sent by either kind of walk. That record is the
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

Some moves don't make it into the trail, because genericMud never sees a
compass direction for them: `enter portal`, a teleport, a mount that carries you
off, or an exit your MUD names in words. After one of those, the way back isn't in
the trail, so drop a new breadcrumb with **Alt+B**.

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
**Triggers**, **Aliases**, **Hotkeys**, **Channels**, **Scripts** or **MUD data**
from the **Show** box. There is no separate simple or advanced builder.

A trigger reacts to text from the MUD. It can play a sound, speak something
shorter, send commands, hide the matched line, interrupt speech, or route the
line to a chat channel. An alias replaces a shortcut you type: `sh` can send
`shoot`, and whatever you type after the shortcut goes along with it, so
`sh goblin` sends `shoot goblin`. A hotkey runs commands when you press a chosen
key. These editors use ordinary fields; no code is required.

An alias with no `*` in it takes the words after it only when nothing else wants
the command: an alias written with a `*` (`sh *` sending `shoot at ${1}`), one
written as a regular expression, or a soundpack's own alias comes first. When
the alias sends several commands, the extra words go on the end of the last one,
unless a command says where they belong with `${1}`, as in `give ${1} to bob`.
The shortcut has to be a word of its own, so `sh` never takes `shout`.

Each trigger, alias, or hotkey can send one command or a sequence. Put one
command on each line. The whole sequence is filled in before its first command
is sent. Command fields understand matched text (`${1}` or the older `%1`), a
value saved by a script (`${script:target}`), and live data sent by the MUD, such
as MSDP `${mud:HEALTH}` or GMCP `${mud:Char.Vitals.hp}`. If a value isn't
available, none of that sequence is sent and genericMud speaks the problem once.

The speech fields understand the same variables, so a hotkey whose speech is
`${mud:Char.Vitals.hp} health` reads your health out when you press it, with no
commands at all. A value the MUD hasn't sent yet is spoken as "no value for"
and its name, rather than staying silent. A hotkey speaks its answer straight
away, cutting off whatever was being read, the way a review key does, and it
still speaks with self-voice turned off.

### Finding out what the MUD sends

**Ctrl+Shift+V** (Automation menu, MUD variables) lists every value this world
knows: the GMCP, MSDP and MSSP data the MUD has sent this session, one line per
value such as `Char.Vitals.hp, 90, GMCP`, then the values soundpacks and scripts
have saved. Type in **Filter** to narrow the list, and press **Enter** on a line
to copy the reference that reads it, such as `${mud:Char.Vitals.hp}`. **F5**
refreshes the values. The list fills only on MUDs that send GMCP, MSDP or MSSP,
usually once you've logged in.

**Most recent first** puts what the MUD sent last at the top, and each line says
how long ago it arrived, so you can do something in the game, refresh, and see
which values just changed. **Watch this** makes a MUD data rule for the value
you're on (see below).

### Reacting when the MUD sends a value

A MUD data rule, the **MUD data** choice in the Automation Manager, does
something whenever the MUD sends a value over GMCP, MSDP or MSSP, such as your
health. Give it the value's name, like `Char.Vitals.hp` (the variables list
shows the names this MUD uses), then any of a sound, speech and commands, the
same as a trigger. `${value}` is the new value and `${name}` is what arrived, so
speech of `${value} health` reads your health out. **Only when it changes** is on
for a new rule, since many MUDs send your health with every prompt whether it
moved or not. A rule for a package hears everything below it: `Char` hears
`Char.Vitals`, `Char.Status` and the rest, the way Mudlet's GMCP events work.
Scripts get the same with `mud.on_data`, described in the scripting guide.

In the trigger, alias and hotkey editors, **Insert a variable into the speech**
and **Insert a variable into the commands** open the same list; choose a value
and press **Insert** to put its reference where the cursor was in that field.

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

- `/alias sh = shoot` — typing `sh` sends `shoot`, and anything you type after
  it goes along: `sh goblin` sends `shoot goblin`. Case matters, and `sh` has to
  be a word of its own, so `shout` is left alone.
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

Soundpacks that come from a git repository (Miriani-Next, Toastush, Erion,
Chatpack and Atlan's Mystavaria pack) are checked for updates when you connect,
at most once a day, and genericMud tells you when one has something new. Type
`/update` to install it, then reconnect. Only the files that changed are
downloaded. When you've changed one of the pack's files yourself, your copy
stays and the pack's new version goes next to it with `.new` on the end. A
pack's first update can't tell yet which files you changed, so it treats any
settings file that differs (the world file, `.ini`, `.cfg` and the like) that
way. The packs' own update commands work too: `update`, `update check` and
Miriani-Next's Ctrl+U use genericMud's updater, and the packs' settings for
updating by themselves at login or while you're idle work as they did in
MUSHclient.

If the MUD drives the sounds itself (many do, over MSP or GMCP `Client.Media`),
there's nothing to install: genericMud plays what the MUD asks for. Sounds are
looked for in the world's Sounds folder first; when the MUD says where to
download one it doesn't find there, genericMud fetches it once into a per-world
`media` folder next to its settings and plays it from there afterwards. Only
files with a sound's name (.wav, .ogg, .mp3 and the like) are downloaded, never
a web page sent in place of one, each up to 64 MB and 2 GB in all for one world,
and never from addresses on your own network, except the machine the MUD itself
runs on (so a MUD on your own computer or LAN can serve its sounds). To stop a
world downloading sounds, untick **Download sounds the MUD offers** in its
details; sounds already in its folders still play.

Sounds and music play as many times as the MUD asks, and loop only when it says
to. When the MUD gives a sound a caption, a description for players who can't
hear it, the caption appears in the output in brackets, like `[A wolf howls]`,
where the review keys can find it. It isn't spoken.

A MUD can also say where a sound comes from, the way Mudlet 5.1 does it: a door
creaking to the east plays in your right ear while you face north, and in your
left ear once the MUD tells genericMud you've turned to face south. Up, down,
ahead and behind play in the middle, since headphones can't tell front from
back. The MUD can move or fade a sound that's already playing, pause sounds and
carry on with them later, and give a sound a priority so a more important one
isn't drowned out: while it plays, sounds of a lower priority don't start, and
lower ones already playing stop. Moving, fading, turning and pausing work in the native
window; in the web UI a sound plays where it started and can't be paused.

### Pictures

A MUD can put an image on a line — a map, a room picture, a shop's sign. MXP has
no way to describe one, so there is nothing to read out: an image is a file name
and a place on the line, and nothing else.

In the **web UI** a **Show picture** button sits where the MUD put it, and the
picture loads when you press it, never before, so a MUD can't make your browser
fetch anything you didn't ask for. In the **text client** — which has nowhere to
draw one — the image becomes a link instead, the same thing MUSHclient does with
one: the file name is the link, and following it (Alt+Enter, or Enter on that
line in the output) opens the picture in your browser.

Either way the MUD has to say where the file lives. Most MUDs send just a file
name and expect the client to be configured with their images address: a world's
details take it as **Images web address**, and the browser-only `--web` mode takes
it as `--images <address>` (that mode has no saved world, so its settings come
from the command line, like `--sounds`). A MUD that names the address in the tag
itself needs no setting at all. Only `http` and `https` are ever followed, and
never an address on your own network except the machine the MUD runs on.

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
| Ctrl+D | Disconnect, or reconnect a disconnected tab |
| Ctrl+W | Close this tab |
| Ctrl+Tab / Ctrl+Shift+Tab | Next / previous session (says which one) |
| Alt+1..9 / Alt+0 | Switch to that world, in the order the tabs were opened |
| Ctrl+Q | Exit |
| Enter | Standard: send the box; editable mode: execute the current logical line |
| Shift+Enter | Standard native window: add a line; editable mode: execute line/selection in place |
| Up / Down | Standard: command history at a sent message's edges; editable mode: move through document rows |
| Ctrl+Enter | Standard: toggle autoretype; editable mode: focus the existing buffer tail |
| Ctrl+R | Editable mode: complete a prefix from earlier document rows |
| Ctrl+Space / Ctrl+Shift+Space | Complete the word you're typing from recent output |
| Numpad | Compass walking (View menu toggle) |
| Ctrl+M | Self-voice on/off |
| Ctrl+Shift+F | Follow mode (speech interrupts on room movement) |
| Ctrl+I | Interrupt mode (every line barges in) |
| F11 | Stop speech now |
| Esc | Stop hiding password input; in editable entry, insert a blank line |
| Shift+F11 | Stop all sounds (panic) |
| Ctrl+1..9 | Recall the last nine lines |
| Ctrl+0 | Recall the tenth line back |
| Alt+Up / Alt+Down | Review line by line |
| Alt+Left / Alt+Right | Review word by word |
| Alt+Shift+Left / Right | Review character by character |
| Alt+Home / Alt+End | Oldest / newest line |
| Alt+Shift+Enter | Spell the current line |
| Ctrl+Shift+C | Copy all or part of the current line |
| Alt+Enter | Follow the link on this line (Enter does it too in the output) |
| Alt+PageDown / Alt+PageUp | Jump to the next / previous link in the output |
| Alt+T / Alt+C | Last tell / last chat |
| Ctrl+F | Find in the output (while the output has focus) |
| F3 / Shift+F3 | Find next / previous |
| Ctrl+Alt+Left / Right | Previous / next chat channel |
| Ctrl+Alt+Up / Down | Scroll within the current channel |
| Ctrl+Alt+Shift+Left / Right | Word by word in the channel line |
| Ctrl+Alt+1..9, Ctrl+Alt+0 | Recent messages on the current channel (0 is the tenth) |
| Alt+B / Alt+R | Drop a breadcrumb / retrace to it |
| Alt+W / Alt+S | Where am I / stop walking |
| Alt+M | Read the mapped room and where its exits lead |
| Ctrl+P | Manage soundpacks |
| Ctrl+Shift+B | Browse soundpacks online |
| Ctrl+B | Open the Automation Manager |
| Ctrl+Shift+V | List the values the MUD has sent and scripts have saved |
| Ctrl+1..6 in Automation Manager | Show triggers / aliases / hotkeys / channels / scripts / MUD data |
| Ctrl+N in Automation Manager | Create an item in the category being shown |
| Enter / Delete in an automation list | Edit / delete the selected item |
| Enter / F5 in the variables list | Copy the reference / refresh the values |
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
  `diagnostic-*.log` from `logs/diagnostics`.

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
router) with a wxPython native UI, BASS audio (pygame when a build has no BASS),
and an alternate web UI
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

Sound plays through [BASS](https://www.un4seen.com), the library soundpacks are
made for, with its BASS_FX add-on. `uv run python tools/fetch_bass.py build/bass`
downloads them for your platform, checked against pinned checksums; the build
bundles that folder, and `GENERICMUD_BASS_DIR=build/bass` runs the tests against
the real library. Without it, a source checkout plays through pygame.

BASS is by Un4seen Developments and BASS_FX by JOBnik! (Arthur Aminov). BASS is
free for non-commercial use, which genericMud is; selling genericMud, or anything
built from it, needs a BASS licence from un4seen.com. BASS_FX may be passed on
free of charge. Their readmes, with their terms, are in the app's bass folder.

The build also carries THIRD-PARTY-NOTICES.txt: the licences of Python and every
library genericMud runs on, written from the build's own environment by
`tools/third_party_notices.py`.

Windows packaging and running from source: `WINDOWS.md`.
