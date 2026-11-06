# Automation and Lua scripting

This guide explains the Automation Manager first, then Lua scripting. You do
not need to know Lua to make a trigger, alias, or hotkey. The Lua examples are
complete enough to copy when you need them.

All automation belongs to one saved world. An alias or script for one MUD does
not run in your other worlds.

## Start in the Automation Manager

1. Connect to the saved world you want to change.
2. Press **Ctrl+B**, or choose **Automation > Manage automation**.
3. In the **Show** box, choose **Triggers**, **Aliases**, **Hotkeys**,
   **Channels**, **Scripts**, **MUD data**, **Timers**, or **From your scripts**.
4. Choose **New**.

These are categories in one manager, not separate simple and advanced tools:

- A **trigger** reacts to a matching line from the MUD.
- An **alias** replaces a shortcut you type with one or more commands.
- A **hotkey** runs actions when you press a chosen key combination.
- A **channel** controls whether routed lines are spoken, shown, or allowed to
  interrupt speech.
- A **MUD data** rule reacts when the MUD sends a value over GMCP, MSDP or MSSP,
  such as `Char.Vitals.hp`. `${value}` is the new value and `${name}` is what
  arrived. **Only when it changes** is on for a new rule.
- A **script** contains reusable Lua automation.
- A **timer** acts on a clock: every so often, or once a day at a time such as
  `03:00`.
- **From your scripts** lists the rules your Lua made, so you can see and switch
  them off without editing the script that owns them.

Choose an item and press Enter to edit it. **Duplicate** opens a copy that you
can change. **Disable** keeps an ordinary item saved without letting it run.
Delete asks for confirmation. The manager remembers the category you last
used.

Keyboard shortcuts inside the manager:

- **Ctrl+1** through **Ctrl+8** show the categories in order.
- **Ctrl+N** creates an item in the category being shown.
- **Enter** edits the selected item.
- **Delete** deletes the selected item after confirmation.
- **F2** renames the selected script.

## Make an alias without writing code

1. Show **Aliases** and choose **New**.
2. In **When I type**, enter a shortcut such as `combo *`. The asterisk catches
   the text after `combo`.
3. In **Commands to send**, enter one command per line:

   ```text
   stand
   kill ${1}
   consider ${1}
   ```

4. Add an optional spoken confirmation, then choose **OK**.

Typing `combo goblin` now sends `stand`, `kill goblin`, and `consider goblin`.
The older `%1` spelling still works, but `${1}` also works in Lua and is easier
to use consistently.

A shortcut with no asterisk takes whatever you type after it, too. An alias for
`sh` that sends `shoot` turns `sh goblin` into `shoot goblin`. The extra words
go on the end of the last command, unless a command has `${1}` in it to say
where they belong. This only happens when no other alias wants the command, so
an alias for `sh *`, or one from a soundpack, still comes first. Lua's
`mud.alias` doesn't do this: a script's alias matches exactly what it says.

## Make a trigger without writing code

1. Show **Triggers** and choose **New**.
2. Enter the text to watch for. **The line contains this text** is usually the
   right match choice. Wildcards, exact whole lines, and regular expressions
   are also available.
3. Fill in any actions you need: commands, spoken text, a sound, speech
   interruption, hiding the line, **Leave this line out of the session log**, or
   routing it to a channel. Hiding the line decides what you hear; leaving it out
   of the log decides what is kept, and either can be set without the other, so a
   repeating notice can be read once and never written down.
4. Choose **OK**. The trigger works on the next matching line.

Hotkeys use the same command field. Focus **Press the key combination**, press
the keys you want, and enter one or more commands. A trigger, alias, or hotkey
can use these values in a command:

- `${1}` is the first wildcard or regular-expression capture. `${2}` is the
  second. A named regular-expression capture can be used as `${target}`.
- `${script:name}` reads a value saved with `mud.set_var` by any script or
  compatible soundpack in this world.
- `${mud:HEALTH}` or `${mud:Char.Vitals.hp}` reads live data sent by the MUD.

genericMud fills in the complete command sequence before sending its first
command. If one value is missing, none of the sequence is sent. This avoids
running half of an action. genericMud speaks each distinct missing-value problem
once and records repeated occurrences in the diagnostic log. One item can send
at most 100 commands at a time.

## Put rules in an order

When several rules match the same line, **Order** decides which is tried first: the lower
number first, and rules sharing a number fall back to their pattern text, so the order never
depends on which file they came from. **Keep checking the rules after this one** is on by
default, so every matching rule acts; clearing it makes that rule the last one heard about the
line.

A player rule that stops the search never stops a soundpack's rules: MUSHclient draws the same
line between one plugin and the next, so a pack's own triggers keep working.

## When Lua is useful

Ordinary trigger, alias, and hotkey fields already support multiple commands
and variable values. Use a Lua script when the automation needs one of these:

- a decision, such as healing only when health is low;
- a timer;
- changing or saving a variable;
- reusable functions or several rules that work together;
- custom speech, sound, or cross-session behavior.

Scripts and field-based items can work together. A script can save a value and
an ordinary alias can use that value in `${script:name}`.

## Open the Lua editor

1. Open the Automation Manager with **Ctrl+B**.
2. Show **Scripts**.
3. Choose **New**.
4. Enter a name such as `10-basics.lua`.
5. Paste or type your script.
6. Choose **Save and reload**.

The `.lua` ending is added if you leave it out. A saved script starts working
immediately. You do not need to disconnect.

If the code is not valid, genericMud explains that it was not saved and leaves
your text in the editor. If a group of scripts cannot reload, the last working
group keeps running.

In the **Scripts** category, choose **Open scripts folder** if you prefer
another text editor. After editing a file outside genericMud, choose
**Reload scripts** in the same category.

## Your first alias

An alias turns a short command into an action. This alias makes `sc` send
`score` to the MUD:

```lua
mud.alias("sc", function()
    mud.command("score")
end)
```

`mud.alias("sc", ...)` tells genericMud what to watch for in the command box.
`function()` starts the action. `end` finishes it.

Lines beginning with two dashes are comments. genericMud ignores them:

```lua
-- This is a comment for the person reading the script.
```

## Your first trigger

A trigger watches text received from the MUD. This one speaks a shorter warning
when the MUD sends a hunger message:

```lua
mud.trigger("You are hungry", function()
    mud.speak("Hunger warning", "system", true)
end)
```

The final `true` tells speech to interrupt what it is currently saying.

## Send one command or several commands

Use `mud.command` for commands that go directly to the MUD.

One command:

```lua
mud.command("look")
```

Several commands as a list:

```lua
mud.command({
    "stand",
    "wield sword",
    "kill rat"
})
```

A list is the clearest form and is recommended for new scripts. You can also
separate commands with semicolons or line breaks:

```lua
mud.command([[stand;wield sword
kill rat]])
```

genericMud checks and fills in every command in the group before it sends the
first one. If a value is missing, none of the commands are sent. One call can
contain at most 100 commands.

There are three related command functions:

- `mud.command(text)` fills in `${variables}` and sends one or more commands.
- `mud.send(text)` sends one literal command. It does not fill in variables or
  split semicolons.
- `mud.execute(text)` handles the text as if you typed it. This means another
  alias can catch it before it reaches the MUD.

Use `mud.command` in most scripts.

## Use text caught by an alias or trigger

An asterisk catches any amount of text. The first asterisk is `${1}`, the
second is `${2}`, and so on.

This alias makes `hit rat` send three commands that use `rat`:

```lua
mud.alias("hit *", function(line, captures)
    mud.command({
        "stand",
        "kill ${1}",
        "consider ${1}"
    })
end)
```

The same value is also available as `captures[1]`. Use that form when the
script needs to inspect or change the value:

```lua
mud.alias("greet *", function(line, captures)
    local person = captures[1]
    mud.echo("Greeting " .. person)
    mud.command("say Hello, ${person}", {person=person})
end)
```

In Lua, `local person` creates a temporary value, and `..` joins pieces of text.
The table `{person=person}` gives the temporary value to `mud.command`.

A question mark catches exactly one character:

```lua
mud.alias("door ?", function(line, captures)
    mud.command("open door ${1}")
end)
```

## Remember a script value

Use `mud.set_var` when a value should be available later:

```lua
mud.set_var("my_attack", "backstab")
```

Read it in Lua:

```lua
local attack = mud.get_var("my_attack", "kill")
```

Or put it into a command with `${script:name}`:

```lua
mud.alias("attack *", function(line, captures)
    mud.command("${script:my_attack} ${1}")
end)
```

Script values are saved for this world when its tab closes. They are available
again the next time the world opens. They are also shared by all scripts and
soundpacks in that world. Give your values distinctive names, such as
`my_attack` or `healer_autoheal`, to avoid using the same name as another
script.

Remove a saved value with:

```lua
mud.delete_var("my_attack")
```

These are the same values a rule's **Send it to** reaches when it says *a script variable*:
that target stores the matched text under a name without any Lua, and a script reads it with
`mud.get_var` like any other. One trigger remembering who paged you, one alias reading it, no
code in between.

## Read data sent by the MUD

Some MUDs send structured data to the client. The common protocols are GMCP,
MSDP, and MSSP. Examples include current health, mana, room information, and
server details. The exact names depend on the MUD.

Read a value in Lua with `mud.get_mud_var`:

```lua
local health = mud.get_mud_var("HEALTH", 0)
local gmcp_health = mud.get_mud_var("Char.Vitals.hp", 0)
```

The second argument is the value to use when the MUD has not sent that item.

Put MUD data directly into a command with `${mud:name}`:

```lua
mud.alias("reporthp", function()
    mud.command("gt My health is ${mud:Char.Vitals.hp}")
end)
```

You can name the protocol when two protocols use the same name:

```lua
local msdp_health = mud.get_mud_var("msdp.HEALTH", 0)
local gmcp_health = mud.get_mud_var("gmcp.Char.Vitals.hp", 0)
local server_name = mud.get_mud_var("mssp.NAME", "unknown")
```

MUD data is not normally available while scripts first load. Put code that
needs it inside an alias, trigger, hotkey, or timer. The data becomes available
after the MUD sends it. If your MUD does not support GMCP, MSDP, or MSSP, these
values will not exist.

To see the top-level names your MUD has sent, add this temporary alias:

```lua
mud.alias("mudvars", function()
    for name, value in pairs(mud.mud_vars()) do
        mud.echo(name .. " = " .. tostring(value))
    end
end)
```

Tables are shown as `table: ...` by this simple example. Once you know a table's
name, use a dotted path such as `Char.Vitals.hp` to read an item inside it. Your
MUD's GMCP or MSDP documentation should list the paths it sends.

## React when the MUD sends data

`mud.on_data` runs a function each time the MUD sends a GMCP package, an MSDP or
MSSP variable, or a value inside one. It runs after the new value is stored, so
`mud.get_mud_var` and `${mud:...}` already read the new value.

```lua
mud.on_data("Char.Vitals.hp", function(name, hp)
    if tonumber(hp) < 100 then
        mud.play("sounds/low-health.ogg")
    end
end, {changes = true})
```

- The function gets the name that arrived and its value. A table stays a table,
  so `value.hp` works on a whole `Char.Vitals` message.
- A package hears everything below it, the way Mudlet's GMCP events do: `"Char"`
  runs for `Char.Vitals`, `Char.Status` and the rest, with the name of the one
  that arrived.
- A path inside a message, like `"Char.Vitals.hp"`, runs only when that message
  carries it, and gets just that value.
- `{changes = true}` skips a value that's the same as the last one this function
  heard. Without it, the function runs every time the MUD sends the data, and
  many MUDs send `Char.Vitals` with every prompt.
- Start the name with `gmcp.`, `msdp.` or `mssp.` to hear only that protocol.
  Names match in any case.

## Combine captures, script values, and MUD data

One command group can use all three sources:

```lua
mud.set_var("my_finisher", "backstab")

mud.alias("finish *", function(line, captures)
    local target = captures[1]

    mud.command({
        "${script:my_finisher} ${target}",
        "gt Used ${script:my_finisher} on ${target} at ${mud:Char.Vitals.hp} health"
    }, {target=target})
end)
```

In this example:

- `${target}` comes from the temporary `target` value;
- `${script:my_finisher}` comes from `mud.set_var`;
- `${mud:Char.Vitals.hp}` comes from the MUD.

If health has not arrived, neither command is sent. This prevents half of a
command group from running by mistake.

You can leave the source off, as in `${target}`. genericMud then checks in this
order:

1. the current captures and temporary values;
2. saved script values;
3. MUD data.

Use `${script:name}` or `${mud:name}` when you want to be clear about the source
or when two sources use the same name.

### Find the names your MUD uses

Every MUD names its data differently, so look before you type. Press
**Ctrl+Shift+V** for the list of every value the MUD has sent this session and
every value scripts have saved, each with the reference that reads it. The
**Insert a variable** buttons in the trigger, alias and hotkey editors put a
chosen reference straight into a field.

### Speak a value without writing code

Speech fields take the same variables as command fields. A hotkey with no
commands and the speech `${mud:Char.Vitals.hp} health, ${mud:Char.Vitals.mp} mana`
reads both out when you press it. Nothing is sent to the MUD. Where a command
with a missing value is not sent at all, speech says "no value for" and the
variable's name instead, so a key you press always answers.

## Make a decision

Lua uses `if`, `elseif`, and `else` for decisions:

```lua
mud.alias("healthcheck", function()
    local health = tonumber(mud.get_mud_var("Char.Vitals.hp", 0))

    if health < 25 then
        mud.command("drink healing potion")
    elseif health < 60 then
        mud.speak("Health is getting low")
    else
        mud.echo("Health is fine")
    end
end)
```

`tonumber` turns text into a number so Lua can compare it with `25` and `60`.

## Use regular expressions when wildcards are not enough

Most aliases and triggers only need `*` and `?`. A regular expression gives
more control. Add `{regex=true}` after the callback.

This trigger catches a name and a number:

```lua
mud.trigger([[(?P<name>\w+) hits you for (?P<damage>\d+) damage]],
    function(line, captures)
        mud.speak(captures.name .. " hit for " .. captures.damage)
    end,
    {regex=true}
)
```

Named groups use the form `(?P<name>...)`. Read them as `captures.name` or use
`${name}` in `mud.command`.

Triggers search anywhere in a received line. Regular-expression aliases start
at the beginning of what you typed. Use `^` and `$` when the whole text must
match:

```lua
mud.alias([[^sc$]], function()
    mud.command("score")
end, {regex=true})
```

genericMud stops a regular expression that takes too long to match. That rule
is disabled for the rest of the session, and the diagnostic log records why.

## Hotkeys

This binds F2 to `score`:

```lua
mud.key("f2", function()
    mud.command("score")
end)
```

Modifiers are joined with plus signs, for example `ctrl+shift+k`. genericMud's
menu keys, editing keys, and important built-in shortcuts remain reserved. Pick
a combination that is not already used by the app or your screen reader.

If two scripts bind the same key, the script loaded later wins.

## Timers

Timers run once after a number of seconds:

```lua
mud.timer(2.5, function()
    mud.command("look")
end)
```

To repeat, start the next timer at the end of the callback:

```lua
local function check_score()
    mud.command("score")
    mud.timer(60, check_score)
end

mud.timer(60, check_score)
```

Reloading or deleting the script cancels its waiting timers.

A timer that repeats, can be found by name, and can be switched off is `mud.add_timer`. The
options table is where its shape goes:

```lua
mud.add_timer("tick", 60, function()
    mud.command("score")
end, {every=60, group="checks"})

mud.enable_timer("tick", false)  -- off, by name
mud.delete_timer("tick")
```

`every` is the gap between fires. `offset` is MUSHclient's head start: the number of seconds
*before* the interval is up that the first fire comes, which is how two timers on one interval
take turns instead of both going off at once. `group` puts it in a group with other rules, so
one call switches them all. `while_disconnected` is MUSHclient's ActiveWhenClosed: the timer
keeps its clock when the world drops, which only makes sense for what a rule can do without a
MUD to send to -- a note, a log line, a value, a script.

A named timer that does not ask to keep going waits for the world to come back and then carries
on from that moment. A one-shot `mud.timer` is gone: a dropped connection cancels it, because
there is nothing to resume it into.

For a timer at a time of day, use a rule in the Automation Manager (Ctrl+B): its **At this
time each day** field takes `03:00`, and the rule can send, speak, note or run Lua.

## Speech, text, and sounds

Speak text:

```lua
mud.speak("The shield spell ended")
```

Speak immediately and interrupt current speech:

```lua
mud.speak("Danger", "system", true)
```

Put text in the output without sending it to the MUD:

```lua
mud.echo("Automation is on")
```

Play a sound from this world's `sounds` folder:

```lua
mud.play("sounds/alarm.wav", "alerts", 0.8, 0, false)
```

The arguments after the filename are channel, volume, stereo position, and
loop. Volume is normally from `0` to `1`. Stereo position is from `-1` for left
to `1` for right. The final value says whether the sound loops.

Stop that sound channel:

```lua
mud.stop("alerts")
```

Use a different channel for sounds that may play at the same time. A new sound
on a channel replaces the old sound on that channel.

## Route matching lines to a channel

Channels let you give some lines different speech and display rules. First make
a custom channel:

```lua
mud.set_channel("combat-notices", {
    speak=true,
    display=true,
    interrupt=true
})
```

Then route matching lines to it:

```lua
mud.trigger("Your shield fades", nil, {channel="combat-notices"})
```

Use a callback instead of `nil` when the trigger should also perform an action.
The built-in channels `main`, `system`, `tell`, and `review` cannot be changed by
a script because they carry important accessible output.

## Work with more than one open session

These functions are useful when several character tabs are open:

```lua
mud.send_to("Healer", "follow Tank")
mud.broadcast("gt Ready")
```

`mud.send_to` handles the command as if it were typed in the named session.
`mud.broadcast` sends it to every other open session. Aliases in the receiving
session can handle either command.

List the open session names:

```lua
for index, name in ipairs(mud.sessions()) do
    mud.echo(index .. ": " .. name)
end
```

Share a temporary value between open sessions:

```lua
mud.shared_set("group_target", "dragon")
local target = mud.shared_get("group_target")
```

Shared values last only while genericMud is running. Use `mud.set_var` for a
value that should be saved with one world.

## Use several script files

Scripts load in alphabetical order, into **one** Lua state for the world. Numbered
names make that order obvious:

```text
10-settings.lua
20-combat.lua
30-healing.lua
```

The files are parts of one program, so a global or a function defined in one is
visible in the ones after it:

```lua
-- 10-settings.lua
function warn_low_hp()
    mud.speak("health low")
end
```

```lua
-- 20-combat.lua
mud.trigger("You are bleeding", function()
    warn_low_hp()
end)
```

Two consequences worth knowing. A name has to be defined **before** it is used,
which is why the numbers are there; and a later file that defines the same global
name wins, as it would if you read the files in order. Keep module-wide values
together in the lowest-numbered file and this never comes up.

`local` is still local to its own file, and `mud.set_var` / `mud.get_var` are still
the way to keep a value across a reload; `mud.shared_get` / `mud.shared_set` share
between worlds.

Aliases with higher `priority` are checked first. The same option works for
triggers:

```lua
mud.alias("heal *", function(line, captures)
    mud.command("cast major heal ${1}")
end, {priority=100})
```

That one state is also the one a rule's **send to script** uses, so a rule can call a
function one of your files defines, and a value a rule leaves behind is there for the
next rule.

## MUSHclient's names also work here

Your world's scripting space carries MUSHclient's own function names beside `mud.*`, in the
same space rather than a second one: the same scripts, the same variables, the same rules.
That is for two people in particular. One learned from MUSHclient's documentation and knows
`Send` and `GetVariable` better than anything else. The other has automation they made in
MUSHclient, which until now only worked there. Both can write it here without translating it
first.

**Which to use.** Write `mud.*`. It is this client's own vocabulary, a rule built with it
takes a function instead of a block of flags, and it is the only one that speaks, follows the
data the MUD sends, and reaches your other sessions. MUSHclient's names are for the other
direction: pasting something, following a MUSHclient guide, or reading a soundpack. Where
both exist they mean the same thing -- `Send` and `mud.send`, `GetVariable` and `mud.get_var`
-- so mixing them is never wrong, only repetitive.

The ones worth knowing:

| MUSHclient | What it does here |
| --- | --- |
| `Send("look")` | sends one command, as `mud.command` does |
| `Note("text")`, `Tell("text")`, `ColourNote(...)` | shows a line in the output window and speaks it |
| `print("text")` | shows the line and never keeps it in the session log, where `Note` does; Lua's `print` goes to the output window, not to a console nobody can see |
| `GetVariable`, `SetVariable`, `DeleteVariable` | the same values `mud.get_var`, `mud.set_var`, and `mud.delete_var` use |
| `DoAfter(seconds, "command")` | runs a command later |
| `AddTrigger`, `AddAlias`, `AddTimer`, and their `Enable…` and `Delete…` | rules registered while the script runs |
| `PlaySound`, `StopSound`, `Sound` | the same sound bus `mud.play` uses |
| `world.Send(...)`, `world.Note(...)` | the same functions through the `world` table, the way a soundpack calls them |

The constants such code tests are there too: `trigger_flag`, `sendto`, and
`error_code`, along with `bit`.

Three things to know about it:

- It is Lua 5.1, the dialect MUSHclient embeds, so pasted MUSHclient code behaves
  as its author wrote it. Both `unpack` and `table.unpack` work.
- Nothing about the safety limits changes. No files, no network, no programs:
  MUSHclient's names are a vocabulary, not more power.
- A name you misspell is an error, spoken the first time it happens and written to the
  diagnostic log. A soundpack gets a quiet do-nothing stand-in instead, because it has
  to load; your own script would rather be told.

Two things it does not include. MushReader's commands, such as `tts_interrupt`, are
aliases a soundpack assumes are installed in MUSHclient; your world does not, so a
command of that name still goes to the MUD. Use `mud.speak` for speech. And a loaded
soundpack is a scripting space of its own: both answer the MUD's lines, but a pack's
variables, triggers, and plugins belong to the pack, and a pack never sees `mud.*`.

## A complete starter script

This example remembers an attack command, provides two aliases, warns about low
health text, and adds a hotkey:

```lua
-- 10-combat.lua

mud.set_var("combat_attack", mud.get_var("combat_attack", "kill"))

mud.alias("setattack *", function(line, captures)
    mud.set_var("combat_attack", captures[1])
    mud.echo("Attack command set to " .. captures[1])
end)

mud.alias("fight *", function(line, captures)
    mud.command({
        "stand",
        "${script:combat_attack} ${1}",
        "consider ${1}"
    })
end)

mud.trigger("You are bleeding badly", function()
    mud.speak("Bleeding", "system", true)
end)

mud.key("f2", function()
    mud.command("score")
end)
```

After saving it:

- type `setattack backstab` to remember `backstab`;
- type `fight rat` to send `stand`, `backstab rat`, and `consider rat`;
- press F2 to send `score`.

## When a script does not work

Check these in order:

1. Press **Ctrl+B**, show **Scripts**, and choose **Reload scripts**. genericMud
   speaks how many scripts loaded or reads the first reload error.
2. Test a simple alias that calls `mud.echo`. This tells you whether the script
   loaded and the pattern matched.
3. Check spelling and capitalization in the text received from the MUD.
4. If a MUD value is missing, remember that its name is game-specific and it is
   only available after the server sends it.
5. Press **Alt+Shift+D**. genericMud speaks the diagnostic log location and a
   short summary. Script errors and slow regular expressions are recorded there.

Only the first error that happens while a callback runs is spoken, so repeated
errors do not flood speech. Every error is still written to the diagnostic log.

## Safety limits

World scripts are sandboxed. They cannot read or write arbitrary files, start
programs, load native code, or use the network directly. A callback is stopped
if it runs for too long. Timers and saved values also have size and count limits
so a broken script cannot freeze the client.

Do not put commands that send to the MUD at the top level of a script. Top-level
code runs when the script is checked and whenever it reloads. Register an alias,
trigger, hotkey, or timer, and send commands from its callback instead.

## API quick reference

Rules and timing:

- `mud.alias(pattern, callback [, options])`
- `mud.trigger(pattern, callback [, options])`
- `mud.key(key_name, callback)`
- `mud.timer(seconds, callback)`
- `mud.add_timer(name, seconds, callback [, options])`
- `mud.enable_timer(name, true_or_false)`
- `mud.delete_timer(name)`
- `mud.on_data(name, callback [, options])`

The alias and trigger options are `regex` and `priority`, plus `label` and `group`.
A trigger can also use `channel`. A label is the name a rule is switched by --
`mud.set_enabled("shoot", false)` -- and the manager reads it out first; a group is what
`mud.set_group_enabled` switches as one. `mud.key` takes a `group` too, and a trigger or
alias a script makes shows up in the manager under From your scripts, unless it has been
switched off there. The `mud.add_timer` options are `every`, `offset`, `group`, `enabled`
and `while_disconnected`. The only `mud.on_data` option is `changes`.

Commands and output:

- `mud.command(command_or_list [, temporary_values])`
- `mud.send(literal_command)`
- `mud.execute(typed_text)`
- `mud.echo(text [, channel])`
- `mud.speak(text [, channel [, interrupt]])`

Variables and MUD data:

- `mud.set_var(name, value)`
- `mud.get_var(name [, default])`
- `mud.delete_var(name)`
- `mud.get_mud_var(name [, default])`
- `mud.mud_vars()`

Sound and channels:

- `mud.play(file [, channel [, volume [, stereo_position [, loop]]]])`
- `mud.stop([channel])`
- `mud.music(file [, channel])`
- `mud.set_volume(channel, volume)`
- `mud.mute(channel [, true_or_false])`
- `mud.set_channel(name, options)`

Several open sessions:

- `mud.sessions()`
- `mud.send_to(session_name, command)`
- `mud.broadcast(command)`
- `mud.shared_get(name)`
- `mud.shared_set(name, value)`
