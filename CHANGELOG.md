# Changelog

Each release's section here becomes the body of its GitHub release, which is the
text the in-app update dialog reads out. Write it for someone deciding whether to
update, not for someone reading the diff. Tagging a version with no section here
fails the build on purpose.

Entries start at 0.7.1. Earlier releases were tagged before this file existed.

## Unreleased

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
