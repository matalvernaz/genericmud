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
Press Ctrl+N, type a name, host, and port, and press Enter. "When I press OK"
offers three things: save and connect, save without connecting, or connect
without saving. Save without connecting adds the world to your list and opens
no tab, then Ctrl+O connects when you want. A good first MUD is Aardwolf:
host aardmud.org, port 4000.
If the MUD is down, or the connection drops, genericMud keeps trying every five
seconds and says why it couldn't connect. File menu, Connection settings turns
that off or changes how long it waits; Ctrl+D stops it for one tab.
If accented or non-Latin letters come out garbled, edit the world and choose
its Encoding. Russian MUDs often use Windows-1251 or KOI8-R, Chinese ones GBK
or Big5. Automatic suits most MUDs.

Playing:
Type commands in the command box and press Enter. Try: look, north (or just
n), say hello, and help. From an empty box, Up and Down step through what
you've typed; a command you're still writing is left alone. Shift+Enter starts
another line, and Enter sends every line as its own command in Standard mode.
View, Command input mode, Editable command buffer (VIPMud-style) selects another
native input mode for new sessions. Standard is the default; open sessions keep
their current mode and drafts. The web window keeps its existing input.
In the editable buffer, Up and Down move through old commands and unsent drafts.
Down at an unfinished tail adds a blank line without sending the draft.
Enter executes one logical line; older lines are copied to the tail.
Shift+Enter executes the line or selected text in place, preserving the selection.
A multi-line selection sends its nonblank lines as separate commands.
Executing an old line keeps other drafts separate, including text ending in a tab.
Moving rows ends word completion rather than carrying its old text into another draft.
Adjacent completed duplicates are removed, including manual repetitions, once
three line separators exist; very short buffers keep duplicates.
Escape inserts a blank row; in the first row it splits at the caret.
Ordinary Backspace at the start preserves the row boundary; Delete and selected
text deletion remain native. There is no Line Entry Mode Off setting yet.
Returning from Output, typing there, or pressing Ctrl+Enter goes to the existing tail.
Emoji typed from Output stay intact on Windows.
Ctrl+R recalls earlier rows by prefix, including drafts. Pack/user keys still win.
On macOS, use Command+R and Command+Enter for those editable-mode shortcuts.
Ctrl+Enter does not toggle autoretype in editable mode; it keeps its saved value.
Passwords use a separate field. Unhiding clears any unsent password and says so.
The buffer is in memory until the session closes and is not automatically trimmed.
Windows/NVDA, GTK/Orca, VoiceOver and IME still need validation for
this mode. VIPMud script-brace assembly is not implemented.
A semicolon sends several commands at once, as in n;n;look; type it twice to
send a real one. Each world can change the separator or turn it off in its
details (Ctrl+O, then Edit), which also has its logging settings.
"Keep soundpack notes in the session log" there is off unless you turn it on: the
lines a soundpack shows itself, whether a MUSHclient pack's Note or a VIPMud
pack's #Say, are shown and spoken either way, and this decides whether they are
also written to the log. A note your own scripts make is always kept, since you
wrote it.
A world's details hold its username and password too, kept in your computer's
own credential store, plus how that login is sent and any commands to type once
you're connected.
Tab moves between the output box and the command box; typing while in the
output box drops you back into the command box.

More than one MUD:
Ctrl+N opens another new world and Ctrl+O opens another saved world, each in
its own tab. Ctrl+Tab switches tabs and says which world you landed in, and
Alt+1 to Alt+0 go straight to the first to tenth tab by its place, which is the
order you opened them until you move one, unless the world's soundpack or your own
hotkeys use those keys. Alt+Shift+Up and Alt+Shift+Down move the tab you're in a
place earlier or later, and Alt+Shift+1 to Alt+Shift+0 put it straight in a place,
0 being the tenth; each says where it landed, and a tab already at either end says
so. Both moves are in the File menu. On a Mac the numbered keys are left free,
since Option and a number types characters there. Out of the box only the tab
you're on speaks — the others keep playing quietly, triggers and all; View menu,
Silence other worlds, lets them all speak. View menu, World name in the window
title, puts the world you're in into the window's title.

Making it talk your way:
Ctrl+Shift+F is follow mode: when you move rooms, speech skips straight to the
new room. Ctrl+I makes every line interrupt (fast fights). View menu,
Background silence keeps genericMud quiet while you're in another window, and
Silence other worlds, in the same menu, decides whether the worlds behind the one
you're looking at speak too.
Ctrl silences the voice, as it does for your screen reader (F11 works too);
Shift+F11 stops every sound.

Walking:
The numpad is a compass: 8 north, 2 south, 4 west, 6 east, corners are the
diagonals, 5 or 0 look, period scans, minus up, plus down (turn this off in
the View menu if NVDA needs your numpad). Type .3n2e to speedwalk. Alt+B
drops a breadcrumb and Alt+R walks you back to it.
With Numpad compass walking on, custom numpad hotkeys are inactive.
Turn it off under View to use numpad keys in Automation hotkeys (Ctrl+B).
Your screen reader or operating system may intercept some numpad keys.

Mapping:
On MUDs that tell the client which room you're in, genericMud draws a map as
you explore. Alt+M reads the room out with where each exit leads and which
exits you haven't been through yet. Type /goto followed by a room name to walk
there, /label followed by a name to name the room you're in, and /map to hear
how much you've mapped.

Automation and sounds:
Ctrl+B opens the Automation Manager. Its Show box contains Triggers, Aliases,
Hotkeys, Channels, Scripts, MUD data, and Timers. A trigger watches text from the MUD. An alias
replaces a shortcut you type. A hotkey reacts to a chosen key combination. A timer
acts on a clock. Each can send one command or several commands, one per line,
without writing code. The Scripts category has the Lua editor for decisions,
timers, saved values, and reusable automation. Ctrl+Shift+B browses ready-made
soundpacks.

Sharing:
File menu, Export This World saves your whole setup — connection, triggers,
sounds, and automation scripts — as one zip. A friend imports it with File
menu, Import a World.

The full shortcut list is under Help, Keyboard Shortcuts.
"""

SCRIPTING = """\
Automation and Lua scripting

Start here:
Connect to the saved world you want to change, or delete it from the same dialog.
Press Ctrl+B or open the Automation menu and choose Manage automation. In the Show
box, choose Triggers, Aliases, Hotkeys, Channels, Scripts, MUD data, Timers,
or From your scripts.
They are categories in one manager.

Make an alias without code:
Show Aliases and choose New. In When I type, enter combo *. In Commands to send,
put these commands on separate lines:
stand
kill ${1}
consider ${1}

Choose OK. Typing combo goblin now sends all three commands, with goblin in
place of ${1}. The older %1 spelling still works.

An alias with no * in it also takes whatever you type after it, when no other
alias wants the command: an alias sh sending shoot turns sh goblin into shoot
goblin. The words go on the end of the last command, unless a command has ${1}
in it to say where they belong.
An alias has the same Ignore capitals box a trigger does, so it is worth clearing
when the capitals in what you type are the point: a rule written brady finds
Brady as well, where one written Brady with the box cleared does not.

Make a trigger without code:
Show Triggers and choose New. Enter the text to watch for. The line contains
this text is usually the right match choice. "Ignore capitals" is ticked on a new
rule, so a rule written brady finds Brady pages: clear it only when the capitals
are the point. A rule you saved before this version keeps matching capitals
exactly until you tick the box for it, which is how MUSHclient and VIPMud match.
"Send it to" says where the response
goes: the MUD, your command box (unsent, to finish yourself), the output as a note
(always kept in the log), the output as a print (never kept in the log), the log alone,
this client again, a script -- a function name, or Lua code such as
mud.set_enabled("combat", false) -- or a script variable, which stores the text under
a name you give it so ${script:name} can read it later. The manager's list says where
each rule's text goes: send look, note Backup started, run script on_backup, store %1 in
variable pager, and for an alias, sh * sends shout %1. Add any spoken text, sound, line
handling, or channel routing you need, then choose OK. Leave this line out of the
session log is separate from hiding it: the line still works as it did, it just never
reaches the file, which is how a repeating notice stops filling the record.
The trigger editor asks the common questions first: the text to match, the commands
and where they go, the speech, and what happens to the line. Its Label, Group, Order
and Keep checking come after the channel, and the sound fields come last.

A trigger or alias can have a name, a group, and an order. The Label is the name a
script turns that one rule off and on by, with mud.set_enabled. The Group switches every
rule that shares it together, with mud.set_group_enabled, which is how all the combat
rules go off in one call; every kind of rule takes one, so a hotkey, a timer and a MUD
data rule can be in the same group as the triggers. Order decides which is tried first
when several match: the lower number first, and equal numbers fall back to the pattern
text. A rule you have not ordered sits above a soundpack's rules, as your rules always
did. Keep checking the rules after this one is on for a trigger and off for an alias,
since your first matching alias has always taken the command alone; clearing it makes a
trigger the last one heard about the line. A player trigger that stops the search never
silences a soundpack. The manager's list says a rule's order beside its kind when you have
set one, so two rules can be compared without opening them.

A timer (Timers in the Automation Manager) acts on a clock rather than on a line:
every so often, or once a day at a time such as 03:00. What it does is the same
question the other rules answer, so it can send commands, fill your command box, write
a note, store a value, or run Lua. Do it once only makes it a one-shot: it runs once
and is then removed, here and from the saved rules the next time they are saved; until
then it is still in the file, so opening the world again runs it once more. An
offset is how many seconds before its interval is up it fires, so two timers on the
same interval take turns. It has to be less than the interval, and a timer that fires
once a day takes none: the editor says why and stays open on the offset instead of
saving something else. Keep running while the world is disconnected lets it keep its
clock while nothing is connected: then it can still note, log, store a value or run a
script, which is how it speaks, but commands have nowhere to go.
A timer whose commands can't be sent because the world is down says so, once each time.
Other timers pause when the world disconnects and carry on when it is back. While
genericMud is still trying to reconnect, timers keep running.

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
and put its reference in the speech or the commands. Most recent first puts what
the MUD sent last at the top, with how long ago; Watch this makes a MUD data rule.

A MUD data rule (MUD data in the Automation Manager) acts when the MUD sends a
value such as Char.Vitals.hp: a sound, speech, commands, and anything else its Send it
to box says, up to a note, a stored value or a script. ${value} is the new value, so
speaking ${value} health reads your health. Only when it changes is on for new rules.
A hotkey's Send it to box works the same way, so a key can write a note or store a value
instead of sending a command.

From your scripts lists the rules your Lua made with mud.trigger, mud.alias,
mud.key, mud.add_timer or mud.on_data: what each one is, what it matches,
and whether it is on. It can be read and switched off, never edited, because the
script's own text is the only copy of a rule it made. There, Enter, Delete and Ctrl+N
say so instead of acting. A mud.timer is not listed: it has no name, and it runs once.

Choose Duplicate to start from a copy. Disable keeps an item saved without
letting it run, and every editor has the same switch as the Enabled box at its top,
so a rule can be parked while you work on it. A switch a script makes shows here too:
after mud.set_enabled or mud.set_group_enabled the item reads Disabled and its Enabled
box is clear, and that is the state the manager saves. Ctrl+1 through Ctrl+8 change
categories, Ctrl+N creates an item, Enter edits, and Delete deletes after confirmation.
F2 renames a script.

MUSHclient's own names work too:
Your world's scripts, and the code a rule sends to a script, run in Lua 5.1 with
MUSHclient's function names beside mud.*: Send, Note, print, GetVariable and
SetVariable (the same values mud.get_var and mud.set_var use), DoAfter, AddTrigger,
AddAlias, AddTimer, EnableTrigger, PlaySound, the world table, and the constants such
code tests. That is so something written for MUSHclient works here as it stands, and
so its documentation is a usable reference. mud.* is still the shorter way, and the
only one that speaks, follows the data the MUD sends, and reaches your other sessions.
A soundpack keeps its own space: it never sees mud.*, and your scripts never see a
pack's variables.

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
Ctrl+D            Disconnect, or reconnect a disconnected tab
Ctrl+W            Close this tab
Ctrl+Tab          Next session (says which one)
Ctrl+Shift+Tab    Previous session
Alt+1 to Alt+0    Switch to the world in that place in the row
Alt+Shift+Up      Move this tab one place earlier
Alt+Shift+Down    Move this tab one place later
Alt+Shift+1 to 0  Move this tab straight to that place, 0 being the tenth
Ctrl+Q            Exit

Typing (Standard, unless noted):
Enter             Send the command line
Up / Down         Command history (only from an empty box or a sent command)
Enter             Send what is in the box: one command per line, then clear it
Shift+Enter       Start the next command on a new line (native window)
Ctrl+Enter        Toggle autoretype (empty Enter resends the last command)
Ctrl+Space        Complete the current word from recent output
Ctrl+Shift+Space  Complete, cycling backwards
Numpad            Compass walking: 8 2 4 6 and corners, 5 or 0 look,
                  period scan, minus up, plus down (View menu toggle)

Editable command buffer (native window; choose in View, Command input mode):
Enter             Execute the logical line at the start of the selection
Shift+Enter       Execute the current line or selected text without moving it
Up / Down         Move through the document; Down can create a new empty tail
Escape            Insert a blank row; split at the caret in the first row
Backspace         Preserve a row boundary unless text is selected
Delete            Native deletion, including selected row separators
Ctrl+R            Complete the prefix from earlier editable rows, including drafts
Ctrl+Enter        Focus the existing tail; does not toggle autoretype in this mode
Pack/user keys on Ctrl+R, Ctrl+Enter or Escape override these mode defaults.
While input is hidden, Escape keeps its unhide action instead.

Speech:
Ctrl+M            Self-voice on or off
Ctrl+Shift+F      Follow mode: speech interrupts when you change rooms
Ctrl+I            Interrupt mode: every line barges in
Ctrl or F11       Stop speech now
Shift+F11         Stop all sounds (panic)
Esc               While the MUD hides what you type (a password), stop hiding it

Reviewing output:
Ctrl+1 to 9       Recall the last nine lines, newest first
Ctrl+0            Recall the tenth line back
Alt+Up / Down     Review line by line
Alt+Left / Right  Review word by word
Alt+Shift+Left / Right   Review character by character
Alt+Home / End    Oldest / newest line
Alt+Shift+Enter   Spell the current line character by character
Ctrl+Shift+C      Copy all or part of the current line: a box opens with the
                  whole line selected, so Enter copies it all
Alt+PageDown / Alt+PageUp   Jump to the next / previous link in the output;
                  Alt+Enter then follows it
Alt+Enter         Follow the link on this line: the keyboard form of clicking it
                  (Enter does the same while the output box has focus). On a
                  line with several links, Alt+Left / Right into one first
Alt+T             Repeat the last tell
Alt+C             Repeat the last chat line

Finding text (Tab to the output first; these do nothing in the command box):
Ctrl+F            Find in the output: what to look for, up or down, match case
F3                Find the next match, same settings
Shift+F3          Find the previous match
A search starts from the line you are on, so moving up the output and searching
again carries on from there. The cursor moves onto the line it finds; a match
older than the lines the output box keeps cannot be reached, and it says so
rather than staying quiet.

Chat channels:
Ctrl+Alt+Left / Right    Previous / next channel
Ctrl+Alt+Up / Down       Scroll within the current channel
Ctrl+Alt+Shift+Left / Right   Word by word in the channel line
Ctrl+Alt+1 to 9          Recent messages on the current channel
Ctrl+Alt+0               The tenth message back, as Ctrl+0 is for the main output

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
/update           Install soundpack updates for this world
Ctrl+B            Open the Automation Manager
Ctrl+Shift+V      List the MUD's variables and script values
Ctrl+Shift+B      Browse soundpacks online
Automation Manager:
Ctrl+1 to 8       Show triggers, aliases, hotkeys, channels, scripts, MUD data,
                  timers, or the rules your scripts made
Ctrl+N            Create an item in the category being shown
Enter / Delete    Edit / delete the selected item
F2                Rename the selected script
Variables list:
Enter             Copy the selected value's reference
F5                Refresh the values
Automation menu   Manage per-world automation; list MUD variables; automation help
Alt+Shift+L       Log this session to a file
Alt+Shift+D       Speak the diagnostic log location and summary
/logs             Where the log files are: world logs, and diagnostics
/secure           Switch this world to the encrypted port the game advertised
"""
