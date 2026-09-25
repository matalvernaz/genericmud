"""In-app help shown from the Help menu.

Plain text, embedded in code rather than read from README.md: the frozen
Windows build doesn't bundle the markdown files, and the Help menu must never
come up empty. One line per fact, no markdown markup — these strings are read
line by line in a text box by screen-reader users. Keep KEYBOARD_SHORTCUTS in
step with config/keymaps/vipmud.toml, the wx menu accelerators, and README.md.
"""

from __future__ import annotations

GETTING_STARTED = """\
Welcome to genericMud.

Connecting:
Press Ctrl+N, type a name, host, and port, and press Enter. "Save this world"
is checked by default; next time press Ctrl+O and choose it from the saved
world list. A good first MUD is Aardwolf: host aardmud.org, port 4000.
If accented or non-Latin letters come out garbled, edit the world and choose
its Encoding. Russian MUDs often use Windows-1251 or KOI8-R, Chinese ones GBK
or Big5. Automatic suits most MUDs.

Playing:
Type commands in the command box and press Enter. Try: look, north (or just
n), say hello, and help. Up and Down arrows step through what you've typed.
Tab moves between the output box and the command box; typing while in the
output box drops you back into the command box.

More than one MUD:
Ctrl+N opens another new world and Ctrl+O opens another saved world, each in
its own tab. Ctrl+Tab switches tabs. Only the tab you're on speaks — the
others keep playing quietly, triggers and all.

Making it talk your way:
Ctrl+Shift+F is follow mode: when you move rooms, speech skips straight to the
new room. Ctrl+I makes every line interrupt (fast fights). View menu,
Background silence keeps genericMud quiet while you're in another window.
Esc silences the voice; Shift+F11 stops every sound.

Walking:
The numpad is a compass: 8 north, 2 south, 4 west, 6 east, corners are the
diagonals, 5 or 0 look, period scans, minus up, plus down (turn this off in
the View menu if NVDA needs your numpad). Type .3n2e to speedwalk. Alt+B
drops a breadcrumb and Alt+R walks you back to it.

Mapping:
On MUDs that tell the client which room you're in, genericMud draws a map as
you explore. Alt+M reads the room out with where each exit leads and which
exits you haven't been through yet. Type /goto followed by a room name to walk
there, /label followed by a name to name the room you're in, and /map to hear
how much you've mapped.

Automation and sounds:
Ctrl+B opens the Automation Manager. Its Show box contains Triggers, Aliases,
Hotkeys, Channels, and Scripts. A trigger watches text from the MUD. An alias
replaces a shortcut you type. A hotkey reacts to a chosen key combination.
Each can send one command or several commands, one per line, without writing
code. The Scripts category has the Lua editor for decisions, timers, saved
values, and reusable automation. Ctrl+Shift+B browses ready-made soundpacks.

Sharing:
File menu, Export This World saves your whole setup — connection, triggers,
sounds, and automation scripts — as one zip. A friend imports it with File
menu, Import a World.

The full shortcut list is under Help, Keyboard Shortcuts.
"""

SCRIPTING = """\
Automation and Lua scripting

Start here:
Connect to the saved world you want to change. Press Ctrl+B or open the
Automation menu and choose Manage automation. In the Show box, choose Triggers,
Aliases, Hotkeys, Channels, or Scripts. They are categories in one manager.

Make an alias without code:
Show Aliases and choose New. In When I type, enter combo *. In Commands to send,
put these commands on separate lines:
stand
kill ${1}
consider ${1}

Choose OK. Typing combo goblin now sends all three commands, with goblin in
place of ${1}. The older %1 spelling still works.

Make a trigger without code:
Show Triggers and choose New. Enter the text to watch for. The line contains
this text is usually the right match choice. Add any commands, spoken text,
sound, line handling, or channel routing you need, then choose OK.

Trigger, alias, and hotkey command fields accept one command per line. ${1}
means the first matched part. ${script:attack} reads a value saved by a script.
${mud:HEALTH} reads live data sent by the MUD. genericMud fills in every command
before it sends the first one. If a value is missing, none are sent and genericMud
speaks the problem once.

Speech fields accept the same variables. A hotkey whose speech is
${mud:Char.Vitals.hp} health reads your health when you press it, without sending
anything. A missing value is spoken as "no value for" and its name. Hotkey
speech is spoken at once, like a review key, even with self-voice off.

Ctrl+Shift+V, or Automation menu, MUD variables, lists every value this world has:
GMCP, MSDP and MSSP data from the MUD, then values saved by scripts. Type to filter,
press Enter to copy a value's reference, and F5 to refresh. In the trigger, alias,
and hotkey editors, the Insert a variable buttons choose a value from the same list
and put its reference in the speech or the commands.

Choose Duplicate to start from a copy. Disable keeps an item saved without
letting it run. Ctrl+1 through Ctrl+5 change categories, Ctrl+N creates an item,
Enter edits, and Delete deletes after confirmation. F2 renames a script.

Open the Lua editor:
Show Scripts and choose New. Enter a name such as 10-combat.lua. Choose Save
and reload when you finish. Scripts belong to one world and load in alphabetical
order. If a save has an error, your text stays in the editor and the working
script is not replaced.

Your first alias:
mud.alias("sc", function()
    mud.command("score")
end)

Type sc in the command box. The alias sends score to the MUD. function starts
the action and end finishes it. A line beginning with two dashes is a comment.

A trigger watches text from the MUD:
mud.trigger("You are hungry", function()
    mud.speak("Hunger warning", "system", true)
end)

The final true makes the warning interrupt current speech.

One command or several commands:
mud.command("look")
mud.command({"stand", "kill rat", "consider rat"})

A list inside braces is the clearest way to send several commands. A semicolon
or line break also separates commands. genericMud checks the whole group before
it sends anything. If a value is missing, none of the commands are sent.

Use mud.send("look") only for one literal command with no variable replacement.
Use mud.execute("sc") to handle text as if you typed it, including aliases.

Use text caught by a wildcard:
mud.alias("combo *", function(line, captures)
    mud.command({"stand", "kill ${1}", "consider ${1}"})
end)

An asterisk catches any amount of text. The first asterisk is ${1} or
captures[1]. A question mark catches one character.

Save a script value:
mud.set_var("attack", "backstab")
mud.command("${script:attack} ${1}")

mud.get_var("attack", "kill") reads the value and can supply a default.
mud.delete_var("attack") removes it. Script values are saved for this world and
shared by its scripts, so use a distinctive name.

Read data sent by the MUD:
mud.get_mud_var("HEALTH", 0)
mud.get_mud_var("Char.Vitals.hp", 0)
mud.command("gt Health is ${mud:Char.Vitals.hp}")

MUD values come from GMCP, MSDP, or MSSP. Their names depend on the game, and
they are available only after the game sends them. ${mud:HEALTH} selects MUD
data. ${script:attack} selects a saved script value. ${1} selects a capture.

A temporary Lua value can also be used:
local victim = captures[1]
mud.command("consider ${victim}", {victim=victim})

When a name has no prefix, genericMud checks captures and temporary values
first, saved script values second, and MUD data third. Use script: or mud: when
you want to make the source clear.

Regular expressions:
Add {regex=true} after an alias or trigger callback. Named groups such as
(?P<target>.+) can be read as captures.target or ${target}. Most scripts only
need the simpler * and ? wildcards.

Other useful calls:
mud.speak(text), mud.echo(text), mud.play(file), mud.stop(channel)
mud.key("f2", function() mud.command("score") end)
mud.timer(2.0, function() mud.command("look") end)
mud.set_channel(name, {speak=true, display=true, interrupt=false})
mud.send_to(session, command), mud.broadcast(command), mud.sessions()

Scripts are sandboxed and time-limited. They cannot read arbitrary files, start
programs, load native code, or use the network directly. Put commands inside an
alias, trigger, hotkey, or timer. Top-level code also runs while the script is
being checked and every time it reloads.

The full step-by-step manual, more examples, troubleshooting, and the complete
API are at:
github.com/matalvernaz/genericmud/blob/main/docs/scripting.md
"""

KEYBOARD_SHORTCUTS = """\
Menus:
Alt+F File.  Alt+P Soundpacks.  Alt+A Automation.  Alt+V View.  Alt+H Help.

Connection:
Ctrl+N            Create and connect to a new world
Ctrl+O            Connect to a saved world
Ctrl+D            Disconnect this tab
Ctrl+W            Close this tab
Ctrl+Tab          Next session
Ctrl+Shift+Tab    Previous session
Ctrl+Q            Exit

Typing:
Enter             Send the command line
Up / Down         Command history
Ctrl+Enter        Toggle autoretype (empty Enter resends the last command)
Ctrl+Space        Complete the current word from recent output
Ctrl+Shift+Space  Complete, cycling backwards
Numpad            Compass walking: 8 2 4 6 and corners, 5 or 0 look,
                  period scan, minus up, plus down (View menu toggle)

Speech:
Ctrl+M            Self-voice on or off
Ctrl+Shift+F      Follow mode: speech interrupts when you change rooms
Ctrl+I            Interrupt mode: every line barges in
Esc or F11        Stop speech now
Shift+F11         Stop all sounds (panic)

Reviewing output:
Ctrl+1 to 9       Recall the last nine lines, newest first
Alt+Up / Down     Review line by line
Alt+Left / Right  Review word by word
Alt+Shift+Left / Right   Review character by character
Alt+Home / End    Oldest / newest line
Alt+Shift+Enter   Spell the current line character by character
Alt+T             Repeat the last tell
Alt+C             Repeat the last chat line

Finding text (Tab to the output first; these do nothing in the command box):
Ctrl+F            Find in the output: what to look for, up or down, match case
F3                Find the next match, same settings
Shift+F3          Find the previous match

Chat channels:
Ctrl+Alt+Left / Right    Previous / next channel
Ctrl+Alt+Up / Down       Scroll within the current channel
Ctrl+Alt+Shift+Left / Right   Word by word in the channel line
Ctrl+Alt+1 to 9          Recent messages on the current channel

Walking:
.3n2e             Speedwalk (3 north, 2 east)
..3n2e            Walk it one room at a time, stopping if blocked
Alt+B             Drop a breadcrumb
Alt+R             Retrace back to the breadcrumb
Alt+W             Where am I (GMCP MUDs)
Alt+S             Stop walking

Mapping (MUDs that report rooms over GMCP or MSDP):
Alt+M             Read this room and where its exits lead
/goto <room>      Walk to a room you've mapped
/label <name>     Name the room you're in
/map              How much of this world you've mapped

Tools:
Ctrl+P            Manage soundpacks
Ctrl+B            Open the Automation Manager
Ctrl+Shift+V      List the MUD's variables and script values
Ctrl+Shift+B      Browse soundpacks online
Automation Manager:
Ctrl+1 to 5       Show triggers, aliases, hotkeys, channels, or scripts
Ctrl+N            Create an item in the category being shown
Enter / Delete    Edit / delete the selected item
F2                Rename the selected script
Variables list:
Enter             Copy the selected value's reference
F5                Refresh the values
Automation menu   Manage per-world automation; list MUD variables; automation help
Alt+Shift+L       Log this session to a file
Alt+Shift+D       Speak the diagnostic log location and summary
"""
