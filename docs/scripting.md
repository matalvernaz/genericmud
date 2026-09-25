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
   **Channels**, or **Scripts**.
4. Choose **New**.

These are categories in one manager, not separate simple and advanced tools:

- A **trigger** reacts to a matching line from the MUD.
- An **alias** replaces a shortcut you type with one or more commands.
- A **hotkey** runs actions when you press a chosen key combination.
- A **channel** controls whether routed lines are spoken, shown, or allowed to
  interrupt speech.
- A **script** contains reusable Lua automation.

Choose an item and press Enter to edit it. **Duplicate** opens a copy that you
can change. **Disable** keeps an ordinary item saved without letting it run.
Delete asks for confirmation. The manager remembers the category you last
used.

Keyboard shortcuts inside the manager:

- **Ctrl+1** through **Ctrl+5** show the five categories in order.
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

## Make a trigger without writing code

1. Show **Triggers** and choose **New**.
2. Enter the text to watch for. **The line contains this text** is usually the
   right match choice. Wildcards, exact whole lines, and regular expressions
   are also available.
3. Fill in any actions you need: commands, spoken text, a sound, speech
   interruption, hiding the line, or routing it to a channel.
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

Scripts load in alphabetical order. Numbered names make that order obvious:

```text
10-settings.lua
20-combat.lua
30-healing.lua
```

Each file has its own Lua state. A Lua `local` or global created in one file is
not visible in another file. Use `mud.set_var` and `mud.get_var` to share a value
between files.

Aliases with higher `priority` are checked first. The same option works for
triggers:

```lua
mud.alias("heal *", function(line, captures)
    mud.command("cast major heal ${1}")
end, {priority=100})
```

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

The alias and trigger options are `regex` and `priority`. A trigger can also use
`channel`.

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
