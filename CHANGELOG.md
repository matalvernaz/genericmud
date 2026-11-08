# Changelog

Each release's GitHub release carries its section here, followed by the earlier
ones under their headings, and the in-app update window shows the versions a
player doesn't have yet. Write each section for someone deciding whether to
update, not for someone reading the diff. Tagging a version with no section here
fails the build on purpose.

Entries start at 0.7.1. Earlier releases were tagged before this file existed.

## 0.27.0 — 2026-11-08

**Ignore capitals**, from MasterJake00

* Triggers and aliases have an Ignore capitals box (Alt+Y), so a rule written `brady` also
  catches "Brady pages:". It's ticked on every new rule. Rules you saved before this
  version keep matching capitals exactly, the way MUSHclient and VIPMud match, until you
  tick the box for them.

**Soundpack updates**

* Update in Manage soundpacks now says when a pack's download has nothing new, and leaves
  the pack as it is. Before, it replaced the pack's folder with the same files and said
  Updated, and anything the pack had saved in its own folder was lost. Packs that update
  file by file, like Miriani-Next and Erion, aren't affected.

## 0.26.0 — 2026-11-07

**Timers, groups, an order for your rules, and somewhere to send their text**, from MasterJake00

* The Automation Manager has a Timers category. A timer fires every so often or once a
  day at a time, and does what any rule can: send commands, fill your command box, write
  a note or a print, write the log, store a value, or run Lua. Do it once only makes it a
  one-shot, and an offset lets two timers on one interval take turns.
* A timer pauses when the world disconnects and carries on when it is back. Keep running
  while the world is disconnected keeps its clock instead; commands it can't send are
  dropped, and it says so once each time the world is down.
* Every rule has Send it to: the MUD, your command box (only when it's empty, so a rule
  never replaces what you're typing), the output as a note, which is kept in the session
  log, or as a print, which isn't, the log alone, a script variable, or a script. A rule
  can store what it caught, and `${script:name}` reads it anywhere a command is written.
* Triggers, aliases, hotkeys, timers and MUD data rules take a Group, which a script can
  switch all at once. Triggers and aliases also take a Label, an Order (lower is tried
  first) and Keep checking the rules after this one. Rules you haven't ordered work as
  before: yours come before a soundpack's, and your first matching alias takes the command.
* A trigger can leave its line out of the session log without hiding it.
* Every rule editor has an Enabled box. A rule a script switched off reads as Disabled in
  the manager, and From your scripts lists the rules your Lua made, with Enable and Disable.
* Text from the MUD never becomes code or a genericMud command. A rule's Lua reads the match
  as `line` and `wildcards`, and a capture that would type a genericMud command, or a second
  command, into your command box is refused, and you're told why.

**Your world's scripts**, from MasterJake00

* A world's script files share one Lua space, so a function in one file can be called from
  another, or by name from a rule.
* Your scripts can use MUSHclient's names, like Send, Note, GetVariable, AddTrigger and
  DoAfter, beside `mud.*`, so code written for MUSHclient works as it stands.
* World scripts now run Lua 5.1, the version MUSHclient uses. A script written with newer Lua
  syntax, like `//` for whole-number division or `<<`, won't load until it's changed, and
  genericMud says which file and line. Soundpacks are unchanged.

**Moving tabs**, from MasterJake00

* Alt+Shift+Up and Alt+Shift+Down move the world you're in one place earlier or later in
  the row of tabs, and Alt+Shift+1 to Alt+Shift+0 put it straight in a place. Both moves are
  in the File menu, and each says where the tab landed. Alt+1 to Alt+0 go to a tab by its
  place in the row. On a Mac the numbered keys are left free.

**Automation Manager keys**

* In the manager the hotkey list is now Alt+K, since H is Automation help; the script list
  is Alt+C, since S is the Show box; and Enable uses Alt+B, like Disable. In the MUD data rule
  editor, Choose from the values the MUD has sent is Alt+R.
* The trigger editor asks the common questions first, with the label, group and order after
  them, and each timer field has its own label and letter.
* None of this has been tried with a live screen reader yet.

**For contributors**

* AGENTS.md and CONTRIBUTING.md spell out what review kept finding, and how to answer it:
  fixes as new commits, one reply per round. A test checks every automation dialog for a
  label before each field and a letter of its own for each control.

## 0.25.0 — 2026-11-06

**An optional editable command buffer**, from have-you-met-matt

* View, Command input mode, Editable command buffer (VIPMud-style) lets you
  keep earlier commands and several unsent drafts in one editable document.
  Standard remains the default. The choice is saved for newly opened tabs;
  existing tabs keep their mode and text. This is for the native window only.
* Enter sends the current line and goes to an empty tail. Sending an older
  line leaves it in place and appends a copy. Shift+Enter sends the current
  line, or just the selected text, without moving or changing it. A selection
  across several lines sends its nonblank lines separately.
* Up and Down move through the document. Down on an unfinished final line
  adds an empty tail without sending the draft. Escape inserts a new line
  without deleting selected text. Backspace at the start of a line preserves
  its boundary; selected text and Delete still use ordinary native editing.
* Ctrl+R completes a prefix from earlier rows, including drafts; press it
  again to cycle. Ctrl+Enter returns to the existing tail, including from
  Output. On macOS, use Command+R and Command+Enter. Soundpack and user
  bindings on these keys still take priority.
* Passwords use a separate labelled field and stay out of the document and
  command history. Unhiding clears an unsent password and says so. Each tab
  has its own buffer, kept in memory until the tab closes, without automatic
  deletion of old drafts.
* The editable mode still needs live screen-reader and input-method testing.
  The web window keeps its existing input behavior.

**Typing fixes**

* On Windows, moving to the next line no longer leaves the caret inside a
  line ending, which could join the next draft onto the previous command.
  Emoji typed from Output are kept intact in both input modes.
* On GTK, typing from Output now keeps the first character, its case and
  keyboard layout, then puts focus in the command box.
* Sending an older line keeps a draft ending in a pasted tab separate from
  the executed copy. Moving rows ends word completion so an earlier line's
  suffix cannot be carried into another draft.

**For contributors**

* The native window and shared session code are split into smaller modules,
  with purpose descriptions and a code map in the contributor guide. This
  internal reorganization does not change how you play.

## 0.24.0 — 2026-11-06

**Reconnecting by itself**, from have-you-met-matt

* If the MUD is down when a tab opens, or the connection drops, genericMud
  keeps trying every five seconds until the MUD is back, however long that
  takes. It says why it couldn't connect, such as "connection refused", and
  says it again if the reason changes.
* File, Connection settings turns this off or on for every world and sets how
  long to wait between tries, from 1 to 300 seconds. With it off, Ctrl+D still
  reconnects when you ask.
* Typing a command while it's trying again says "Still connecting" and keeps
  the command in the box, instead of asking whether to reconnect.
* A connection that keeps dropping as soon as it's back is announced once,
  not every time, and "Reconnected" waits a second to be sure it held.

**Connecting**, from MasterJake00

* A tab takes its host, port, TLS switch and encoding from the saved world
  each time it connects, so editing a world's port and pressing Ctrl+D uses
  the new port. The sounds folder still needs a new tab.
* When a game says over MSSP that it has an encrypted port, genericMud
  mentions it once when you connect, and /secure moves the world onto it and
  reconnects.
* Understand Pueblo HTML, a new switch in a world's details, off unless you
  turn it on: a Pueblo world's HTML is read as plain words, and the commands
  it offers as links become links you can follow.

**The saved world list**, from MasterJake00

* Edit can rename a world while no tab is connected to it. Its map, its
  automation rules and its saved soundpack settings go with the new name.
* Delete removes a saved world and its login, after asking. Its logs, map and
  rules stay. Alt+D, or the Delete key in the list (delete on a Mac).
* A new world can be saved without connecting to it: When I press OK offers
  Save and connect, Save, don't connect, and Connect, don't save.

**Typing and reading**, from MasterJake00

* The command box holds several lines. Enter sends, Shift+Enter starts a new
  line, and a pasted list goes out one command per line, in order. Enter on an
  empty box still sends a blank line, for "press return to continue".
* Up and Down only bring back earlier commands from an empty box or from a
  command you've already sent. A command you're still writing stays as it is.
* A search (Ctrl+F, F3) starts from the line you're on in the output, instead
  of from the last match or the newest line.
* Enter on an output line with several links follows the one under the caret.

## 0.23.1 — 2026-11-05

**Reconnect fixes**, from have-you-met-matt

* Ctrl+D, or Disconnect on the File menu, now stops genericMud while it's
  waiting to reconnect or still reaching the MUD. Before, it said "Still
  connecting" and carried on trying. The menu says Disconnect while a
  connection is on its way, so you can tell which one Ctrl+D will do.
* Closing a tab while it's connecting or reconnecting stops it straight away.
  Before, a tab you'd just closed could still connect to the MUD a moment
  later.
* While genericMud is waiting to reconnect, a soundpack that asks whether
  you're connected now gets no, and soundpacks that run on a timer, like
  Erion's, pause until the connection is back.

## 0.23.0 — 2026-11-05

**Reacting when the MUD sends a value**, ideas from Mudlet

* The Automation Manager has a sixth choice, MUD data: a rule that plays a
  sound, speaks, or sends commands whenever the MUD sends a value over GMCP,
  MSDP or MSSP, such as your health. Speech of ${value} health reads your
  health out. Only when it changes is on for a new rule, since many MUDs send
  your health with every prompt whether it moved or not. Ctrl+6 shows them.
* The MUD variables list (Ctrl+Shift+V) can put what arrived most recently at
  the top, each line saying how long ago, so you can do something in the game
  and see which values just changed. Watch this makes a MUD data rule for the
  value you're on.
* Scripts get the same with mud.on_data, which runs a function each time a
  package, a variable or a value inside one arrives.

**Sounds from a direction**, from Mudlet 5.1

* A MUD can say where a sound comes from: a door creaking to the east plays in
  your right ear while you face north, and the sounds swing round when the MUD
  says you've turned. It can also move or fade a sound that's already playing,
  pause sounds and carry on later, and give a sound a priority so a less
  important one doesn't drown it out. Moving, turning and pausing work in the
  native window; in the web UI a sound plays where it starts.

**Jumping between links**, from Mudlet

* Alt+PageDown jumps to the next link in the output and Alt+PageUp to the
  previous one, skipping lines without any. The cursor lands on the link, so
  Alt+Enter follows it. In a world whose soundpack uses those keys, like
  Erion's history browser, they stay the soundpack's.

**Reading what MUDs send**

* Chat that IRE games such as Achaea send over GMCP, with colour codes inside,
  is read properly now; before, none of its parts could be used. Two rarer ways
  servers send GMCP are read properly too.
* An MSDP value sent as a list, like your active spells, keeps every item, not
  just the first, and one stray marker in a message no longer loses everything
  after it. A server that lists its variables that way now gets asked for them.

## 0.22.0 — 2026-11-05

**MXP links that do more**, from MasterJake00

* A link can start a command for you to finish, such as a name that starts a
  tell to that player. Following it puts the start of the command in the
  command box and sends nothing until you press Enter. A link that would type
  a genericMud command, or more than one command, is refused and says why.
* A link can offer a menu, such as buying or examining something in a shop.
  Following it asks which one you meant: a numbered list, where a number key
  picks a row, or buttons along the bottom in the web UI. Escape sends nothing.
* When a MUD gives a link a caption saying what it does, you hear the caption
  as the review cursor arrives on the link, and again with the command when you
  follow it.
* MUDs that keep their own values for the session, such as your hit points,
  show the values in the text instead of their names, and links can use them.
  A new connection starts without the old one's values, one of the reconnect
  problems have-you-met-matt reported.

**Aliases take the rest of the line**, an idea from MUDBall

* An alias with no * in it takes whatever you type after it: an alias sh that
  sends shoot turns sh goblin into shoot goblin. The words go on the end of the
  last command, or wherever a command puts ${1}. An alias with a *, one written
  as a regular expression, or a soundpack's own alias still comes first, and
  typed /alias shortcuts work the same way.

**Copying**, also from MUDBall

* Ctrl+Shift+C copies from the line you're reviewing, or the newest line. A
  small box opens with the whole line selected, so Enter copies all of it, or
  select just a name or a number first.

**Logging in**, from MasterJake00

* The MUSH and MOO login is now sent as connect name password in lowercase, the
  way connect screens print it. Some servers ignore it in capitals and leave you
  sitting at the connect screen with no error.

**Thanks**

Thanks to everyone who has contributed to genericMud so far:

* MasterJake00: MXP from the start, logins kept in your computer's credential
  store, reconnecting a tab, switching worlds by number, letting other worlds
  speak, the world in the window title, echoing what you type, separate log
  folders, the tenth recall and channel message, and all the MXP work in this
  release.
* fastfinge: the move to prism and uv, playing the sounds a MUD sends over
  GMCP and downloading the ones you don't have, and speech for dashes, curly
  quotes and CJK text.
* have-you-met-matt: numpad hotkeys with the compass off, accented and other
  non-English text kept readable in saved rules and world exports, keeping your
  command when you type from the output, and a careful report of reconnect
  problems.

## 0.21.3 — 2026-11-02

**Typing from the output keeps your command**

* Starting to type while you're reading the output still moves you to the
  command box, but no longer throws away what you'd already typed there. On Mac
  and Linux, the first letter used to replace the whole unsent command; it now
  goes in where you left off.

## 0.21.2 — 2026-11-02

**Other worlds keep quiet**

* A world in another tab no longer cuts off the one you're listening to. With
  follow mode on, every room change in a background world stopped the speech of
  the world in front, and a soundpack in a background world that stops speech
  on its own triggers did the same. While genericMud is in the background with
  Background silence on, it no longer cuts off your screen reader in other
  programs either.

## 0.21.1 — 2026-11-02

**The Mac download**

* 0.21.0 was published without its Mac download. This is the same release with
  the Mac one included; on Windows and Linux nothing changes from 0.21.0, so if
  you already have that, there's nothing new for you here.

## 0.21.0 — 2026-11-02

**Logging in by itself**

* A world's details take a username and password, kept in your computer's own
  credential store (the Windows Credential Manager, the macOS Keychain, or the
  Secret Service on Linux) and never in genericMud's files. Connect chooses how
  they're sent: in answer to the MUD's own prompts, as MUSH and MOO's CONNECT
  command, as a Diku name and then password, or only when the MUD asks for them
  over MXP. A new world starts on No auto-connect.
* Sent after connecting takes commands to type once you're in, like look and
  score, one per line.
* A password the MUD echoes back is kept out of the output, the speech and the
  logs.
* On a computer with no working credential store, genericMud keeps logins in
  its own credentials file and says so when it starts.

**MXP**

* MUDs that use MXP get it. Their markup is taken out of the text instead of
  reaching you as words, and genericMud answers their questions about what it
  supports. Each world can switch this off.
* Links a MUD puts in its text can be followed: Alt+Enter follows the link on
  the line you're reviewing, and so does Enter on a line in the output box. In
  the web UI they're real links.
* A picture becomes a link in the text client, which opens it in your browser,
  and a Show picture button in the web UI. A world's details take the MUD's
  images web address, for MUDs that send only a file name.
* MXP is only read once the MUD offers it, and nothing another player writes
  can make genericMud follow a link, load a picture, send your login or hold
  back your output.

**More than one world**

* Alt+1 to Alt+9 go straight to a tab, and Alt+0 to the tenth, unless the
  world's soundpack or your own hotkeys use those keys. On a Mac they're left
  free, since Option and a number types characters there.
* Ctrl+Tab and those keys say which world you landed in.
* View, Silence other worlds: switch it off and every open world speaks, not
  just the one in front. It's on unless you change it, so nothing starts
  talking unasked.
* View, World name in the window title, names the window after the world
  you're in.

**Keys**

* Ctrl+Alt+0 reads the tenth message on the channel you're browsing.
* With numpad compass walking off, numpad keys can be your own hotkeys.
* On a Windows keyboard with an AltGr key, AltGr and a key types its character
  instead of being taken as a shortcut: AltGr+0 is @ on a French keyboard and }
  on a German one.

**Each world's own settings**

* Echo input in the output puts each command you send into the output as well,
  so the review keys can read it back after it has scrolled away. It's never
  spoken, and nothing is shown while the MUD is hiding what you type.

**Logs**

* Session logs and diagnostic logs now have folders of their own, logs/worlds
  and logs/diagnostics, and /logs says where they are. The first start of this
  version moves the logs already there, and leaves anything it doesn't
  recognise where it is.

## 0.20.5 — 2026-10-28

**Faster with big soundpacks**

* genericMud gets through each line about four times faster than before. With a
  big soundpack, a long burst of output, like a full status screen or a busy
  fight, used to hold up what you typed next for a second or more; it goes out
  straight away now. Sound packs fire exactly as before.

**A smaller download, and a tidier folder**

* The Windows download is about 15 MB smaller: it no longer carries libraries
  genericMud never uses.
* Updating now removes the files earlier versions left behind. The first start
  after this update tidies genericMud's program files, which can free 40 MB or
  more if you've updated many times. Your worlds, soundpacks and logs aren't
  touched.
* Diagnostic and crash logs from more than 30 days ago are cleared out, keeping
  the last 20 of each. The logs of your games are never cleared.

**Small fixes**

* After you install a soundpack, the window that confirms the world says the
  pack's sounds are in place, and the Sounds folder box says that leaving it
  blank uses the soundpack's own sounds. A blank box with nothing said about it
  read as if the world had no sounds.
* Connecting says "1 soundpack loaded" rather than just "1 soundpack".
* In Browse Soundpacks, the Download and set up button no longer reads as
  "Download and and Set Up".

## 0.20.4 — 2026-10-27

**Updating genericMud**

* When you've skipped some versions, the update window now tells you what's new
  in each of them, newest first, not just in the newest. This time it shows
  every version's notes, so read down until you reach the one you have. From
  the next update on it stops there by itself, and leaves out the stars and
  other formatting marks.

## 0.20.3 — 2026-10-27

**Licences**

* genericMud comes with the licences of Python and every library it runs on, in
  THIRD-PARTY-NOTICES.txt, as those licences ask. BASS's terms are in its bass
  folder.

**Soundpacks**

* Toastush's index command, a tool for the pack's maintainers, no longer goes to
  the MUD.

## 0.20.2 — 2026-10-27

**Soundpacks**

* Miriani-Next's room ambience plays. On its default setting it plays only while
  its window is in front, and genericMud never told it when that was. Packs now
  hear when their world comes to the front or goes behind, as in MUSHclient: you
  switching tabs or switching to another program. Chatpack's option to keep
  sounds out of the background, and the Yu-Gi-Oh pack's option for sounds while
  its window isn't in front, work too.
* Packs hear that the MUD is connected once the connection is open, so what a
  pack sends as it connects reaches the MUD. A connection that fails is no longer
  announced to them, so Mystavaria and Krinnzenn's pack don't start their music
  for a MUD that's down.

## 0.20.1 — 2026-10-27

**Finding in the output**

* After a Find, going back to the output puts you at the newest line, and F3
  now searches from there too. It used to carry on from the line the last search
  found. Searching backwards from the bottom finds the newest match; searching
  forwards finds nothing past the bottom.

## 0.20.0 — 2026-10-27

**A new sound engine**

* genericMud now plays sound through BASS, the sound library MUSHclient
  soundpacks are made for. It plays every kind of sound file the vault's packs
  ship, including MP3 inside WAV files and, on Windows and Mac, Chatpack's .m4a
  sounds, which the old engine couldn't play. On Linux the .m4a sounds still
  don't play. If BASS can't start, genericMud uses its old sound engine.
* Sounds can change pitch, fade, slide their volume and pan, pause and carry
  on, and start or finish partway through, as packs and MUDs ask.

**Soundpacks**

* Erion, Mata's Empire, Mystavaria, Cosmic Rage, SquareSoft, the V.I. Mush
  bundle, Krinnzenn's pack and Mush-Z sound as they do in MUSHclient: music and
  ambience fade out instead of cutting off, pitch and pan changes work, and
  their volume keys change the sounds already playing. Cosmic Rage's sounds
  that come without a pan play in the middle instead of far left.
* Miriani-Next, Toastush, Chatpack and the Yu-Gi-Oh pack can pause their
  sounds, slide and pitch them, and tell when one has finished, so Miriani-Next
  no longer keeps every sound it ever played. The Yu-Gi-Oh pack's life point
  sounds play, once for each hundred points lost.
* Chatpack's sound effects work: the echo its env command puts on every sound,
  and the echo, reverb, flanger and gargle ChatMUD asks for.
* VIP Mud packs' #PC Frequency changes a sound's pitch.
* Scripts using MUSHclient's PlaySound get its volume and pan as MUSHclient
  reads them. A volume of 0, MUSHclient's loudest, played silent.

**Sounds the MUD sends**

* GMCP Client.Media sounds start and finish where the MUD says, fade in and
  out, and fade away when the MUD stops them, as in Mudlet.

## 0.19.0 — 2026-10-27

**Soundpack updates**

* Miriani-Next, Toastush, Erion, Chatpack and Atlan's Mystavaria pack are
  checked for updates when you connect, at most once a day, and genericMud
  tells you when one has something new. Type /update to install it, then
  reconnect.
* Updates download only the files that changed, so they take seconds instead
  of fetching the whole pack again. That includes Update from source in the
  soundpack manager.
* Files you've changed are kept: the pack's new version goes next to yours
  with .new on the end. A pack's first update can't tell yet what you
  changed, so it keeps settings files that way, like the world file and .ini
  and .cfg files.
* Miriani-Next's and Toastush's own update commands work: update, update
  quietly, update check, and Ctrl+U in Miriani-Next. Their settings for
  updating by themselves at login or while you're idle work too, instead of
  sending "update" to the MUD. After an update they play their update sounds
  and, if you asked for it, show their changelog.

## 0.18.1 — 2026-10-26

**MUSHclient soundpacks**

* Each plugin in a MUSHclient pack has its own script space, as in
  MUSHclient. Plugins that used the same function names, like the V.I. Mush
  bundle's init_stuff and save_config, ran each other's code instead of their
  own.
* The V.I. Mush bundle no longer sends a line of junk to Materia Magica each
  time the MUD updates your room or character.
* Materia Magica's accelerators plugin binds its hotkeys without announcing
  an error for each one.
* yugioh's volume keys, F9 to F12, and its volume commands work. A pack's
  world script now hears the world open, connect, disconnect and close.
* Mush-Z's logging commands, like mushz logging debug, work.

## 0.18.0 — 2026-10-26

**MUSHclient soundpacks**

* Mush-Z, Miriani-Next and Toastush load all of their triggers. They add most
  of them while starting up, in a way genericMud didn't support, so Mush-Z
  ran with about a tenth of its sounds and Miriani-Next and Toastush with
  almost none.
* Miriani-Next's sounds play. Every one of them stopped on a time check
  genericMud answered wrongly.
* Mush-Z's cast alias no longer sends every spell twice.
* Krinnzenn's help works: sphelp shows its pages, and readme no longer goes to
  the MUD as a command.
* Materia Magica's help commands, like sbrief help, show the plugin's help
  instead of a blank line.
* Triggers that watch for a word the pack keeps in a variable, like Mush-Z's
  channel prefix, now see it.
* A pack's triggers are tried in MUSHclient's order, and one that matches
  stops the rest of its plugin's triggers for the line unless it says to keep
  going. Other plugins' triggers, and your own, still hear every line.
* A switched-off alias, or one in a switched-off plugin, no longer swallows the
  command you typed.
* A pack's timers keep running after the connection drops and comes back.
* Mush-Z's plugin manager can remove a plugin and add it back. It used to stay
  removed.
* Channel histories can copy messages to the clipboard and paste them into the
  command box for you to edit or send.
* Packs that keep logs in MUSHclient notepads can show them, and Soundpacks >
  Soundpack notepads opens any of them. Writing to a notepad never takes your
  focus.
* Packs can open their help pages and links in your browser. Only web and
  email links open; a pack can't start a program this way.
* Packs save their settings when the session closes and whenever they ask to,
  and hear when the connection drops and comes back.

**Changed on purpose**

* A MUSHclient pack's trigger that doesn't say to keep evaluating now stops
  its plugin's later triggers for the line, as in MUSHclient. genericMud used
  to try every one, which could play two sounds for one line.
* When one of a MUSHclient pack's aliases matches what you type, the pack
  decides what's sent, as in MUSHclient. genericMud also sent what you typed
  when the alias said to keep evaluating. An alias that stops evaluation
  stops only its own plugin's aliases, not every plugin's.
* Text a pack's trigger sends is read the way MUSHclient reads it: a
  backslash sequence like \n becomes the character it stands for.

## 0.17.0 — 2026-10-26

**Sounds the MUD sends**

* Sounds and music play as many times as the MUD asks, and loop only when it
  says to. Music used to loop forever, and a sound asked for three times played
  once.
* A world's details have a Download sounds the MUD offers box. Untick it and
  nothing is downloaded; sounds already in the world's folders still play.
* When a MUD gives a sound a caption, a description for players who can't hear
  it, the caption appears in the output in brackets, like [A wolf howls]. It
  isn't spoken.
* A MUD can fetch a sound ahead of time, by asking for it at volume 0, without
  it playing.

**Safety**

* Soundpack downloads check the address they actually reach, so a server's
  answers can't send one to a machine on your own network, and no download
  follows a redirect from https down to plain http.
* A soundpack whose sounds folder is set to a network share no longer has it
  looked at. On Windows, looking would have sent your login hash to that
  machine.

**Also**

* A VIP Mud soundpack downloading its sounds no longer holds up connecting to
  your worlds.

**Changed on purpose**

* Music from a MUD that doesn't say how many times to play it plays once, as
  the sound protocols define, instead of looping.

## 0.16.0 — 2026-10-26

**Speech**

* Lines with em dashes, curly quotes, ellipses, or Korean, Chinese or Japanese
  text are spoken again. Depending on where such a character fell in the line,
  the whole line was silently skipped.

**Each world's own settings**

* A world's details (Ctrl+O, then Edit) set its command separator. It's a
  semicolon unless you change it. Type it twice to send a real one, as in
  `say hi;; bye`, or pick another character, or switch splitting off for a MUD
  that uses semicolons in its commands. Your own aliases, hotkeys and triggers
  split the same way; soundpacks aren't affected.
* A world can start logging by itself when it connects, write a new file each
  session, one per day or one for the world, and leave out what you type. Every
  log adds to its file and starts with the date and time. A log you stop with
  Alt+Shift+L stays stopped until you close the tab.
* Changes to those apply straight away, even to a world that's open.

**Sounds the MUD sends**

* MUDs that send their sounds over GMCP Client.Media, as Mudlet supports, are
  heard now, and so are MSP sounds a MUD offers to download. A sound you don't
  have is fetched once into the world's own media folder and played from there.
  Only real sound files are kept, up to 64 MB each and 2 GB for a world, and
  nothing is fetched from your own network except the machine the MUD runs on.

**Reconnecting**

* Ctrl+D reconnects a tab that's disconnected, keeping its scrollback, and the
  File menu's Disconnect says Reconnect when it would. Pressing Enter on a
  command in a disconnected tab asks whether to reconnect instead of dropping
  it, and leaves what you typed in the box. genericMud's own slash commands
  still run without asking.
* Disconnecting with Ctrl+D is announced the way a dropped connection is.

**Also**

* Ctrl+0 speaks the tenth line back, after Ctrl+1 to 9.
* Saved rules and shared world files keep accented and non-Latin text readable.

**Changed on purpose**

* Two separators in a row send the separator itself, so `n;;s` is now the one
  command `n;s` rather than two.

## 0.15.0 — 2026-10-26

**Soundpack menus and questions**

Soundpacks can show you menus and ask you things, in ordinary dialogs your screen
reader reads. Before, those menus never appeared and the packs carried on as if
you'd cancelled.

* The unofficial Prometheus soundpack's options menu on F2 works: volume, muting,
  and every panning and gagging switch.
* Cosmic Rage's gag creator, decimal separator choice and other menus work.
* Star Conquest's menus for adding and removing chat buffers work.
* Miriani's saved destinations list and buffer choices work.
* MUSHclient soundpacks' message boxes, questions and lists appear. Chatpack asks
  once whether to download sounds it doesn't have.
* With more than one world open, a pack's dialog says which world is asking.
* Keys a pack ties to VIP Mud's own menus and windows go back to genericMud.
  With Cosmic Rage loaded, Alt+F, Ctrl+O, Escape, Ctrl+Q and others did nothing.

**Worth knowing**

* While a pack's dialog is open, genericMud waits for your answer, the way VIP Mud
  and MUSHclient do. The pack's own timers keep going, so hints it speaks while
  its menu is open still play, and your other worlds catch up once you answer. A
  dialog left unanswered for fifteen minutes closes as if cancelled.
* A MUSHclient pack's list starts with nothing selected, as in MUSHclient, so
  pressing Enter without choosing picks nothing.

## 0.14.0 — 2026-10-26

**VIP Mud soundpacks**

genericMud now runs VIP Mud soundpack scripts the way VIP Mud's manual describes,
so several packs that played nothing now work.

* The official Prometheus soundpack plays its sounds and registers with the game.
  It played nothing before.
* The unofficial Prometheus soundpack plays its sounds and remembers your
  settings, and its sounds off, room sounds off and similar keys work. It played
  nothing before.
* Miriani's VIP Mud soundpack loads and plays. It loaded nothing before.
* Sounds are panned the way the packs mean them. A sound slightly to one side
  used to play entirely in one ear.
* Packs save their settings and lists, Star Conquest's buffer list among them.
  They were never written before.
* Triggers match whole lines, as in VIP Mud, so they no longer go off on lines
  that only mention their text.
* When a pack shortens a line, such as Star Conquest's tracking reports, you hear
  the short version. Messages a pack speaks also appear in the output, where the
  review keys can find them.
* Packs' speech mute, find, "read the last few lines" and reload-the-soundpack
  commands work.
* A soundpack can't be made to play or write files outside its own folder and
  your sounds folder, and no pack script can keep genericMud busy for more than
  a couple of seconds.

**Changed on purpose**

* In a VIP Mud trigger, # matches a digit and % a letter, as in VIP Mud. COGG's
  game announcement sound no longer plays, the same as in VIP Mud.

## 0.13.1 — 2026-10-25

**Soundpacks stop typing at the MUD**

* MUSHclient soundpacks written for MushReader speak their messages instead of
  sending them to the MUD. Stellarmush and Mystavaria sent "tts_interrupt" lines
  and the message was never read out.
* Chat lines a soundpack keeps in its channel history no longer go to the MUD as
  "history_add" commands. Empire and Mystavaria sent one for every chat line.
  Those lines are now under their channel for the Ctrl+Alt arrow keys.
* Miriani-Next's Do Not Disturb works.
* The official Prometheus VIP Mud soundpack no longer sends stray "}" lines to
  the MUD after you log in.

## 0.13.0 — 2026-10-25

**Escape no longer stops speech**

* Press Ctrl, as you would for your screen reader, or F11. Escape now has a new
  job at password prompts, below.

**Passwords**

* What you type at a password prompt goes to the MUD exactly as you typed it.
  With auto-repeat on it used to be replaced by your username, and a semicolon
  or a leading dot split it or walked you somewhere.
* Passwords no longer come back when you press Up arrow.
* While a MUD is hiding what you type, Escape stops hiding it. genericMud
  mentions this the second time you type while your typing is hidden.
* MUDs that run on FluffOS now say when the password prompt is over. Before,
  genericMud treated everything you typed afterwards as a password, so aliases,
  speedwalks and slash commands stopped working. If a MUD never says so,
  genericMud stops hiding a minute after your last line, during login only, and
  tells you.

**Soundpacks**

* MUSHclient soundpacks no longer type their scripts and settings at the MUD.
  Empire's sent a page of script on every room, Cosmic Rage's sent tts_interrupt
  lines, and SquareSoft's sent its credits at the login prompt.
* Lines a MUSHclient soundpack prints are now spoken, as MushReader does, so some
  packs have more to say. Empire's death message is one.
* Triggers and aliases match the way MUSHclient and VIPMud match them. Aliases no
  longer swallow ordinary commands: Prometheus's one-letter aliases ate say,
  look, north and quit, and Erion's F10 and F12 both ran the same volume alias.
* MUSHclient soundpack timers now repeat and can be switched on and off, as packs
  expect.
* VIPMud hotkeys with Ctrl, Alt or Shift work. Star Conquest went from 8 working
  hotkeys to all 40, and Cosmic Rage from 11 to all 113.
* A VIPMud soundpack that downloads its sounds as you play, such as Cosmic Rage,
  no longer freezes genericMud while it fetches one.
* Updating a soundpack from its source works for packs that come as a zip of
  zips, such as Miriani's.
* A soundpack can no longer write files outside its own folder.

**Fixes**

* One unusual character in a world's name could empty your saved-world list.
* Pressing Disconnect while genericMud is reconnecting no longer ends with it
  saying "reconnected".
* Walking to a mapped room, or a safe walk typed with two dots, no longer gives
  up or says you've arrived early when the MUD is slow to say which room you're
  in.
* The Automation Manager tells you when a hotkey's key is already taken by
  genericMud, instead of saving a hotkey that would never work.
* A Windows update that couldn't start no longer reports a failed update the
  next time you open genericMud, and old update downloads are cleaned out of
  your temp folder.

## 0.12.0 — 2026-09-25

**MUDs that don't send UTF-8 read correctly**

* Each world has an Encoding setting, in the New World dialog and when you edit a
  saved world (Ctrl+O, choose it, Edit). Leave it on Automatic unless letters
  come out garbled. A Russian MUD usually needs Windows-1251 or KOI8-R and a
  Chinese one GBK or Big5, and there are Central European, Greek, Turkish,
  Baltic, Japanese and Korean choices too. Requested in #5.
* What you type goes out in the same encoding, so commands in Cyrillic reach the
  MUD intact, the letter я included.
* Room names and other information the MUD sends alongside the text use the
  world's encoding too, so Alt+W and the map read correctly.
* On Automatic, a MUD that sends Western European text instead of UTF-8 now gets
  your typing that way as well. Accented letters you typed, such as ñ or é, used
  to arrive as two garbled characters.
* A UTF-8 MUD that sometimes sends a stray byte can be set to UTF-8, so one bad
  line no longer makes the rest of the session read as Western European.

**Hear what the MUD knows, without writing code**

* Automation, MUD variables (Ctrl+Shift+V) lists every value this world has sent,
  such as your health or the room you're in, and the values soundpacks and
  scripts have saved. Type to filter, press Enter to copy the reference that
  reads a value, and F5 to refresh. Requested in #4.
* The speech of a trigger, alias or hotkey can include those values. A hotkey
  whose speech is ${mud:Char.Vitals.hp} health reads your health out when you
  press it, and the editors' Insert a variable buttons put a value in for you.
* Hotkey speech is spoken straight away, cutting off whatever was being read, and
  still speaks with self-voice off, like the review keys.

**Fixes**

* Worlds named in Cyrillic, Chinese or another non-Latin alphabet each keep their
  own triggers, hotkeys, scripts and map. They used to share one set, so
  automation made for one MUD also ran on the others. The first time this version
  starts, it moves each world's files into its own folder. Where several worlds
  used to share one, each gets its own copy, and the shared original is kept
  under a name ending in ~pre-migration.
* Ctrl+Space completes words with accents or in other alphabets, instead of
  skipping them or cutting them off at the first accent.
* Moves you type in full, such as north or southwest, now go on the breadcrumb
  trail, so Alt+R brings you back along them.
* Starting genericMud from a command line takes --encoding, and --sounds now works
  in the normal window too.

## 0.11.0 — 2026-08-13

**Self-voice speaks through your screen reader on Mac and Linux too**

* Speech now goes through prism, which talks to whichever screen reader you use.
  On Windows that adds ZDSR, PC-Talker, System Access, ZoomText and Narrator to
  the NVDA and JAWS support that was already there. On Mac, output speaks in
  your VoiceOver voice, and on Linux in your Orca voice, instead of a separate
  system voice that ignored your speech settings.
* When no screen reader is running, the client still speaks: the Windows voice,
  the built-in Mac speech, or speech-dispatcher on Linux, same as before.
* Lines reach a braille display wherever your screen reader supports it, which
  now includes Mac and Linux; those two used to be speech only.

**Speech that used to stop and stay stopped**

* Restarting your screen reader no longer silences the client. Speech went quiet
  for the rest of the session, without saying why, and only came back if you
  restarted genericMud. It now picks your reader up again on the next line, and a
  reader you start after genericMud is found as well.
* MUDs that restart in place no longer forget you are using a screen reader. On a
  server that strips decoration for screen reader users, that setting was lost at
  the restart and the output filled with symbols again until you reconnected.
* Review keys still speak when self-voice is off with Ctrl+M. Alt+Up and Alt+Down,
  Ctrl+1 to 9, and the last tell and chat keys produced nothing at all in that
  mode, which is the mode you are most likely to be reading in.

## 0.10.0 — 2026-08-07

**genericMud maps as you explore**

* On MUDs that tell the client which room you are in, genericMud now builds a
  map while you play. Alt+M reads out the room and where each exit leads,
  including which exits you have not been through yet.
* Type /goto and a room name to walk there by the shortest way the map knows.
  Type /label and a name to name the room you are in so you can come back to
  it, and /map to hear how much of the world you have mapped. Maps are saved
  for each world.
* Walking to a mapped room goes one room at a time and stops if something moves
  you off the route, such as a teleport or a trapdoor, instead of carrying on
  from the wrong place.
* Only rooms the MUD identifies itself go on the map. A MUD that does not share
  your location says so, rather than building a map that would walk you into a
  wall.

**MUDs that used to sit silent now work**

Four handshakes were missing, and each one made a MUD look broken.

* Soundpack cues over MSP. Servers that wait to be asked, which includes many
  SMAUG-based MUDs, were sending no sounds at all. Reported in #1.
* Music and sound now stop when the MUD says to stop. An off cue used to be
  treated as a file named Off, so entering one area with music meant it played
  over everything else for the rest of the session. Looping ambience such as
  rain and wind loops properly, and music volume is honoured.
* Room and character data over GMCP. Nothing arrived until the client
  subscribed, so "where am I" had nothing to say and careful walking could not
  tell when you had arrived.
* Room and character data over MSDP. Same problem, so every piece of live MUD
  data in a command field came back empty with nothing to explain why.

**genericMud now tells the MUD it is a screen reader**

* It answers the terminal-type question properly, including the screen reader
  flag. MUDs that check it stop sending ASCII art, maps and progress bars.

**It stops talking over your screen reader**

* Download progress no longer speaks percentages. NVDA already follows the
  progress bar itself, so those announcements talked over your own beeps, and
  over a deliberate choice to turn progress output off. The stage it has reached
  is still spoken, because a progress bar cannot say that part.

**Disconnects explain themselves**

* When a MUD closes the connection and says why, whether that is kicked,
  banned, shutting down, or logged in elsewhere, you now hear the reason
  instead of a plain lost connection, and genericMud no longer reconnects
  straight back into a kick.
* Fixed every command being written to the session log as three asterisks after
  a reconnect.

**The manual covers the whole client**

* The README now explains everything genericMud can do, and opens with the four
  keys you need to start playing.
* Walking is documented properly for the first time: how to write a route like
  .3n2e, what ..3n2e does differently, and what the breadcrumb trail can and
  cannot take you back through.

## 0.9.1 — 2026-08-01

**Automation now has one place to work**

* Ctrl+B opens the Automation Manager. Triggers, aliases, hotkeys, channels,
  and Lua scripts are categories in that one dialog instead of separate simple
  and advanced tools.
* New, Edit, Duplicate, and Delete work from the selected category. Triggers,
  aliases, hotkeys, and channels can also be disabled without deleting them.
  Script-only actions such as Rename, Reload Scripts, and Open Scripts Folder
  become available when Scripts is selected.

**Ordinary automation can do more without code**

* A trigger, alias, or hotkey can send one command or several commands, one per
  line.
* Command fields can combine matched text, values saved by scripts, and current
  GMCP, MSDP, or MSSP data. The complete sequence is checked before any command
  is sent.
* The manual and offline help now explain the unified manager, its keyboard
  shortcuts, command variables, and when Lua is useful in plain language.

## 0.9.0 — 2026-08-01

**Simple rules and advanced scripts now share one Automation menu**

* Open Automation, then Visual Rule Builder to make an alias, trigger, or hotkey
  without writing code. Ctrl+B still opens the builder directly.
* Builder rules and scripts belong to the current world and work alongside
  installed soundpacks.

**Per-world Lua scripts can handle larger automations**

* Open Automation, then Edit Scripts for This World to create, edit, rename,
  delete, or reload one or more scripts without reconnecting.
* An alias, trigger, hotkey, or timer can send one command or a complete command
  sequence. Commands can combine captured text, saved script values, and current
  GMCP, MSDP, or MSSP data from the MUD.
* A missing value stops the whole sequence before anything is sent. A script
  that does not save or reload correctly leaves the last working version active.

**Scripts are documented, portable, and safe to share**

* Automation, Scripting Help contains an offline introduction. The repository
  manual adds step-by-step examples, troubleshooting, and the complete scripting
  reference in plain language.
* Export This World now includes its Lua scripts. Importing the world restores
  them with its visual rules and sounds.
* World scripts run in a time-limited sandbox. They cannot read arbitrary files,
  start programs, load native code, or connect to the network directly.

## 0.8.1 — 2026-07-31

**Online soundpacks install from the sources that actually contain them**

* Press Ctrl+Shift+B to open Browse Soundpacks Online from anywhere in the
  client.
* Packs whose Vault download is only an installer or stale web page now use the
  author's real repository or update feed. Downloads are checked before setup,
  alternate published links are tried, and an unavailable author archive is
  reported plainly instead of looking like a genericMud failure.
* Large manifest-based packs download concurrently, resume cleanly after an
  interruption, and never promote a partial file into the active pack.

**MUSHclient packs keep their sound behavior without importing another client**

* Plugins that only provide MUSHclient's windows, updater, mapper, logging, help,
  or speech output are represented as satisfied but skipped. Pack dependency
  managers keep working, and Manage Soundpacks reports every compatibility skip
  separately from a real error.
* Sound plugins can now use the lifecycle, GMCP, plugin-to-plugin calls, HTTP,
  JSON, SQLite, filesystem helpers, audio modules, hotkeys, and world information
  expected by current MUSHclient packs.
* One malformed optional rule, missing optional plugin, or unsupported non-Lua
  helper no longer prevents the rest of the soundpack from loading.

**VIPMud packs find both bundled and remotely managed sounds**

* Installer-style packs such as Cosmic Rage fetch a missing sound from their
  declared sound repository when it is first needed, then reuse the local copy.
  Downloads remain confined to that pack and are size-limited.
* Deferred loaders, nested settings files, Windows paths, and archived files with
  an accidental doubled `.wav` extension now resolve correctly.

**Verified against the live catalogue**

* Every currently retrievable MUSHclient and VIPMud entry in the Soundpack Vault
  installs and activates without plugin, script, module, or lifecycle errors.
  The one author archive that is currently offline is shown as Source unavailable
  and does not affect the other packs.

## 0.8.0 — 2026-07-31

**genericMud now runs on Mac and Linux**

* Every release now ships three builds: the Windows folder, a Mac app
  (`genericMud-macos.zip` — unzip and drag `genericMud.app` to Applications),
  and a Linux folder (`genericMud-linux.tar.gz`). The same client, the same
  soundpacks, on all three.
* Self-voice speaks everywhere: through your screen reader or SAPI on Windows
  as before, through the built-in Mac voice, and through speech-dispatcher on
  Linux (the same speech engine Orca uses).

**Soundpacks behave much more like they did in VIPMud and MUSHclient**

* Variable names in VIPMud packs no longer care about capitalization, so a
  pack that sets `vol` and reads `@Vol` gets your volume instead of full blast.
* `#Stop` actually stops the pack's own sounds, including loops.
* MUSHclient packs get real sound buffers: a hit sound no longer cuts off
  ambience started in another buffer, `StopSound` works, and live pan moves
  a playing sound instead of being ignored.
* Sounds referenced with Windows-style paths, or in folders whose name differs
  only in capitalization, now resolve to the right file — including when two
  folders contain a file with the same name.
* A pack that keeps re-registering temporary triggers no longer slows the
  client down over a long session.

**Your reading position stays where you put it**

* Reviewing scrollback or a chat channel while new lines arrive no longer
  silently moves you onto lines you already heard.
* Output arriving while the Find window is open no longer loses your place.
* Pressing Up to check an old command no longer throws away what you had
  half-typed; Down brings it back.
* The stop keys — Escape and F11 for speech, Shift+F11 for sound — now work
  while you're reading the output box, not only from the command line.

**The client tells you what happened**

* Connecting, enabling or trusting a pack, and adding, changing, or deleting
  a rule in the soundpack builder are all spoken now instead of silent.
* Deleting a builder rule asks first. A rule missing its match text says so
  instead of silently vanishing.
* Disconnect messages are plain language instead of error codes, and an
  untrusted pack now tells you where to trust it.
* On first launch the client welcomes you and names the keys to get started.

**Finding soundpacks is simpler**

* Browse Soundpacks Online only lists packs genericMud can load. A checkbox
  shows packs made for other clients if you want to try one anyway.
* Everything soundpack lives in one Soundpacks menu: manage, browse online,
  set up from a folder, and the builder.

**Safety and reliability**

* Your password is kept out of the session log, command history, and
  auto-repeat while the server hides typing, and auto-login can no longer be
  tricked into typing your credentials by in-game text later in the session.
* A server turning compression off mid-session no longer silences the client
  until you reconnect, a full disk no longer stops output, and disconnecting
  by hand now stops pack music instead of letting it restart itself.

## 0.7.3 — 2026-07-30

**Find now lands the reading cursor on the line it announces**

* On Windows, a successful Find spoke the matched line but parked the cursor
  above it — one character higher for every line of scrollback — so arrowing
  after a search read an unrelated line. The cursor now lands exactly on the
  announced line, at its start, for new searches and F3/Shift+F3 repeats.
* A search result that had just arrived and was not yet painted into the
  output could fail to move the cursor at all; it resolves now.
* A line that merely contained the matched line inside longer text can no
  longer capture the cursor.

## 0.7.2 — 2026-07-30

**Star Conquest communicator sounds actually play**

* The earlier bracket-pattern fix let the communicator trigger fire, but its
  per-channel selector still used VIPMud's `%ifWord` function, which genericMud
  did not understand. Every selector therefore evaluated false before reaching
  its sound.
* `%ifWord`, parenthesized true/false results, `and`, `or`, and `NOT` conditions
  now work. All 11 built-in Star Conquest communicator branches were checked
  against the current 2.8.1.1 pack and resolve their real sound files; the two
  configurable community-channel sounds work as well.

**New worlds and saved worlds have separate dialogs**

* Ctrl+N now opens a focused New World form. Ctrl+O opens an alphabetical
  saved-world list with connection details, Connect, Edit, and New World
  actions. Creating a world saves it by default.
* Host and port errors stay in the form with a useful message instead of
  silently connecting to port 4000. Invalid saved or imported ports are rejected,
  and a damaged worlds file no longer prevents startup.

**Find now finds the first result**

* A new search includes the newest line when searching backward and the oldest
  line when searching forward. This fixes searches in one-line output and terms
  that only occur at a scrollback boundary.
* Reopening Find restarts from the chosen edge. F3 and Shift+F3 remain exclusive
  repeats, so they advance to another match. Find remains scoped to the output:
  Ctrl+F in the command box keeps its normal editing behavior.

**The Windows build opens only the GUI**

* The packaged app now uses the Windows GUI subsystem, so launching
  `genericMud.exe` no longer opens a terminal beside it. The local build script
  now produces the same one-folder, audio-enabled layout as the release build.
* Windowed startup and voice fallback paths no longer assume stdout or stderr
  exists. The alternate web UI chooses free local ports, reports boot failures,
  and closes its engine, socket, and HTTP server cleanly.

**Typing and completion are more reliable**

* Ctrl+Shift+Space can begin completion in reverse in both interfaces. The web
  interface now also learns completion words from output.
* Escape and modified Enter reach the engine in the web interface, while
  copy/paste, browser Find, macOS Command shortcuts, and AltGraph typing remain
  local. Commands entered while its socket is opening are queued after
  authentication instead of disappearing.

**Settings and soundpack updates survive interrupted writes**

* Worlds, preferences, credentials, soundpack indexes, user rules, pack state,
  and updater state now use atomic replacement. Corrupt or malformed rows fall
  back safely instead of crashing startup or turning strings into enabled
  settings.
* Replacing a soundpack is staged and rolls back if copying or index persistence
  fails. The no-code rules editor validates changes before replacing its working
  rules.
* Closing an older tab no longer unregisters a newer tab that happens to use the
  same world name.

**Update prompts carry useful release notes**

* The updater dialog now receives this version's actual changelog section from
  the GitHub release instead of generic installation boilerplate.

## 0.7.1 — 2026-07-27

**Soundpack channel triggers work again**

* VIPMud soundpacks write a literal square bracket in a pattern as `[[]`, which is
  how they match a channel line like `[General Communication] Someone transmits,
  "Hello all."`. genericMud read those brackets as ordinary text, so the pattern
  looked for three characters where the line only had one, and never matched.
* In the Star Conquest pack that silenced 23 triggers: every communicator channel,
  every ship jump, launch, landing, docking and navigate announcement, and the
  combat shot-fired and destruction calls. Nothing was broken in the sound path,
  which is why the failure was invisible. The lines simply arrived and matched
  nothing.
* Any VIPMud pack using bracketed patterns gains the same triggers back.

**Find text in the output**

* Ctrl+F searches the output for a phrase, with a direction setting (up towards
  older lines, or down towards newer) and a Match case checkbox. F3 repeats the
  search and Shift+F3 repeats it the other way. Both settings stick, so reopening
  the dialog and pressing Enter runs the same search again.
* Find only works once you have tabbed into the output. It searches the full
  scrollback, which is far deeper than the visible output holds, so a match can be
  spoken even when it is too old to move the cursor to.
* Follow mode has moved from Ctrl+F to Ctrl+Shift+F to make room.

**Reading back through the output stays put**

* Arriving text no longer throws the cursor to the newest line while you are
  reading back through the output. Tabbing into the output from the command box is
  now the only thing that takes you to the bottom.

**Command history no longer says "blank"**

* Pressing Up in the command box spoke the recalled command over the top of the
  screen reader's own reading of the field, and the two collided as a spurious
  "blank" before every recall. The client now leaves that announcement to the
  screen reader.

**Bursts of output are no longer cut short**

* A room description, a who list or a help page arrives as a single burst, and the
  speech governor treated any burst over twenty lines as a flood, replacing the
  rest with "N more lines". It now absorbs a couple of screenfuls at a time and
  only genuinely sustained spam is summarised.
