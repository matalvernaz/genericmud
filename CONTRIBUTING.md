# Contributing to genericMud

How to make a change that lands quickly: what to run, what review checks first, and how to write the pull request.

genericMud is played by blind and low-vision people, most of them on a screen reader (NVDA, JAWS, VoiceOver or Orca) and the rest on genericMud's own voice. A change is judged first by what those players hear and whether they can reach it from the keyboard. Most review comments come from the rules below, so reading them first saves a round trip.

Changes written with an AI assistant are welcome. You're responsible for what you submit, and the same rules apply. `AGENTS.md` points assistants at this file.

## Before you start

- One change per pull request. Two unrelated fixes are two pull requests.
- If your pull request builds on another open one, start its description with "Stacked on #N", and keep your own commits last so review can read just those.
- For anything big, like a new protocol, a new window, or a change to how input or connections work, open an issue first and say what you plan to do.
- Don't change what players already have without saying so: a default, a file format, what goes in the log, which rule runs first, or the Lua version their scripts run on. If your change does, say it in the first lines of the description, with what happens to someone who updates.

## Setting up and running the checks

Setup is in the README's "For developers" section. Before you open a pull request, run what CI runs:

```sh
uv sync --all-extras --locked
uv run ruff check .
uv run pytest -q
node --test tests/test_frontend_input.mjs tests/test_frontend_ws.mjs tests/test_frontend_menu.mjs tests/test_frontend_copyline.mjs
node --check frontend/src/app.js
```

- The `node --test` line is the list in `.github/workflows/tests.yml`. If you add a front-end test file, add it to that list too.
- Run pytest directly. Piping it through `tail` or `head` hides its exit status, so a failing run can look like a passing one.
- Tests that need wxPython start with `pytest.importorskip("wx")`. They skip on a machine without wx, and CI runs them.
- The app is built for Windows, macOS and Linux. If your change behaves differently on one of them, say so in the pull request.

## What a player hears

1. Nothing fails silently. Every key, command and button either does what was asked or says why it didn't. "Nothing to follow on this line" beats silence.
2. Say why something failed, not just that it did: "Connect failed: host not found", not "Connection unavailable". When something retries, say the reason the first time and again whenever the reason changes, not on every attempt.
3. Never throw away what the player typed. A half-written command, a draft in a dialog, or a field they were editing must survive any key that isn't meant to clear it.
4. Say things once. Don't announce the same thing twice, and don't speak what the screen reader is about to read anyway, like the label of the control that just got focus.
5. Put focus back where the player was. After a dialog, a menu or an action, focus returns to where they started, usually the command box. Don't take focus away from someone who is typing.
6. Every control has a name a screen reader can read. In wx, create the `StaticText` label immediately before its control, and also call `SetName` on the control. `SetName` only works on Windows, so the label's position is what works everywhere. Checkboxes and buttons carry their own label text.
7. Every control in a dialog gets a shortcut key (an `&` before the letter), unique within the dialog and not clashing with the menu bar. A literal `&` in a button label is written `&&`, and other labels don't take that, so better still, write "and".
8. Never use `wx.PD_AUTO_HIDE` on a progress dialog. On Windows it dismisses the native dialog out from under the screen reader, and it has crashed NVDA.
9. Check a new keyboard shortcut against `genericmud/config/keymaps/vipmud.toml`, the wx menu accelerators and the README's "Every keyboard shortcut" list. A soundpack's binding, or the player's own hotkey, on the same key wins over genericMud's, so pick keys popular packs don't use. Shift on its own is never a shortcut, except with a function key, because Shift is for capitals and for selecting text; Ctrl+Shift and Alt+Shift combinations are fine. The keys the command box keeps for editing can't be shortcuts either: the clipboard keys, undo and redo, select all, and moving or selecting by word.
10. genericMud has two windows: the native wx one, and the web one (`--web`, in `frontend/`). Change both, or say in the pull request and the README which one your change covers.

## Anything the MUD sends is untrusted

- Text from the MUD must never turn into a genericMud command. That includes MXP and Pueblo links, menus and prompts, and GMCP and MSDP data. Text that the MUD puts into the command box may not start with the client prefix `/`, may not contain the world's command separator (`;` unless the world changes it), and may not contain control characters, and its length is capped. Check each line, not the whole block: the box sends every line as its own command. `typed_refusal` in `genericmud/app/links.py` is the check links and rules share.
- Never paste MUD text into code. A capture, a GMCP value or a line from the MUD that ends up in Lua is a way for anyone on the MUD to run Lua as the player. Code gets MUD text as values, the way a rule's Lua gets `line` and `wildcards`, never as source text to be parsed.
- Parsers cap their work: sizes, counts, nesting depth, and anything else the server controls. Never compute something whose size the server chooses, like a huge exponent or an unbounded repeat, without a limit.
- Don't send a plain web address to the MUD as a command, and only download what a protocol asks for, like GMCP media, through the existing download checks.

## Threads

The engine runs on an asyncio loop in a background thread, and the wx window runs on the main thread. Never touch wx from the loop thread: hand the call over with `wx.CallAfter`. Long work, like a download, runs off the UI thread.

## Tests

- Every fix comes with a test that fails without the fix. Run it against the commit before your fix, and put the failure in the pull request.
- Test the path the app really takes. If the app loads rules before scripts, so does the test; a test that builds things in a friendlier order can pass while the app is broken.
- Use real wx controls under `xvfb-run` rather than fakes, when what you're testing is what the player sees or where focus goes. A fake that doesn't track which page is shown, or a fake clock whose timers can't be cancelled, can't catch the bugs it is there to catch.
- Test what the player gets: the words spoken, where focus lands, what's in the command box. Not just internal state.
- Give every async wait in a test a timeout, so a regression fails the test instead of hanging the whole suite.
- Undo anything global a test changes, like keyboard layouts, environment variables, or files outside `tmp_path`.

## Docs and help

- Any change a player can notice updates the README, in the section where a player would look for it, and the in-app help in `genericmud/help_text.py`. A new key also goes in the README's "Every keyboard shortcut" list and in `KEYBOARD_SHORTCUTS` in `help_text.py`.
- The in-app help is plain text, read line by line in a text box: one fact per line, no markdown.
- Write for players: what to press, and what they'll hear.
- In a pull request, leave `CHANGELOG.md` alone. The maintainer writes the release notes when a release is made, and they credit contributors by name.

## The pull request

- Title: one plain sentence saying what changes for a player, like "A saved world can be deleted from the world list".
- Description: what was wrong or missing; what a player does and hears now; how you tested it, including the test that failed before your fix; and anything you couldn't test, like a platform or a screen reader you don't have. The pull request template asks for these.

## Answering review

- Push your fixes as new commits on top, and push once when the round is done. On a stack, put the fixes on the top branch rather than rebasing every branch for each one: every push to a stack emails the maintainer once per pull request in it.
- Reply once, on the top pull request of the stack (or on the one pull request), with what you changed for each point. One reply per round, not one per pull request.

## Where things live

The native and web windows share one session engine. A change to command execution,
protocols, soundpacks or speech normally belongs in the shared code, rather than in a window.

- `genericmud/app/engine_app.py` constructs one world's session and owns its connection
  lifecycle and initial state. Import `EngineApp` from `genericmud.app`.
- `genericmud/app/` groups that session's methods by job: `incoming_text`, `typed_commands`,
  `autologin`, `mud_data`, `server_media`, `soundpacks`, `world_files`, `walking`, `links`,
  `key_actions` and `session_log`. These are mixins of `EngineApp`; their docstrings name the
  state they use. `sink.py` connects automation side effects back to the session.
- `genericmud/ui/wx_app.py` starts the native window and its background asyncio loop.
  `main_window.py` holds the frame and menus; `session_panel.py` builds one world's tab.
  The tab's methods live in `session_connection.py`, `command_box.py`, `output_box.py` and
  `pack_window.py`. Dialogs have their own modules in the same folder.
- `genericmud/web_launcher.py`, `genericmud/bridge/` and `frontend/` connect the shared
  engine to the optional web window. `bridge/protocol.py` defines the messages both windows use.
- `genericmud/transport/` handles connections, reconnects and telnet negotiation.
  `genericmud/protocol/` parses server formats such as GMCP, MSDP, MXP, Pueblo and MSP.
- `genericmud/automation/` holds triggers, aliases, timers, hotkeys and channel routing.
  `genericmud/scripting/` exposes the Lua API and interprets VIPMud and MUSHclient scripts.
- `genericmud/packs/` installs, loads, updates and shares soundpacks and user rules.
- `genericmud/voice/` routes speech and controls the speaking queue; `voice/backends/`
  connects it to the player's screen reader or system voice. `genericmud/sound/` handles
  audio playback, buses and server media downloads.
- `genericmud/model/`, `render/` and `review/` hold buffered lines, text rendering and
  output review. `navigation.py` and `mapping.py` handle routes, breadcrumbs and the room map.
  `completion.py` indexes recent output words and cycles through completion matches.
- `genericmud/config/` loads and saves worlds, preferences and keymaps.
  `genericmud/session/` supplies credentials, automatic login, logs and cross-session routing.
  These are standalone services; the methods that connect them live in `app/`.
- `genericmud/update/` checks and installs app updates. `tools/`, `genericMud.spec` and
  `hooks/` build the standalone apps. `genericmud/__main__.py` handles command-line options.

Tests follow those jobs in `tests/`. Native source assertions use `tests/ui_source.py` to find
a named definition across the split files. A monkeypatch must target the module that reads the
name, even when the method is reached through `SessionPanel` or `EngineApp`.

## Licensing

genericMud is MIT licensed. Only bring in code under a licence that allows that, and say where it came from. Ideas from other MUD clients are welcome. Code, tests or data from GPL-licensed clients, like Mudlet, or from copyrighted manuals, like VIPMud's, are not.
