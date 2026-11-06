# Contributing to genericMud

How to make a change that lands quickly: what to run, what review checks first, and how to write the pull request.

genericMud is played by blind and low-vision people, most of them on a screen reader (NVDA, JAWS, VoiceOver or Orca) and the rest on genericMud's own voice. A change is judged first by what those players hear and whether they can reach it from the keyboard. Most review comments come from the rules below, so reading them first saves a round trip.

Changes written with an AI assistant are welcome. You're responsible for what you submit, and the same rules apply. `AGENTS.md` points assistants at this file.

## Before you start

- One change per pull request. Two unrelated fixes are two pull requests.
- If your pull request builds on another open one, start its description with "Stacked on #N", and keep your own commits last so review can read just those.
- For anything big, like a new protocol, a new window, or a change to how input or connections work, open an issue first and say what you plan to do.

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
7. Every control in a dialog gets a shortcut key (an `&` before the letter), unique within the dialog and not clashing with the menu bar. A literal `&` in a button label is written `&&`.
8. Never use `wx.PD_AUTO_HIDE` on a progress dialog. On Windows it dismisses the native dialog out from under the screen reader, and it has crashed NVDA.
9. Check a new keyboard shortcut against `genericmud/config/keymaps/vipmud.toml`, the wx menu accelerators and the README's "Every keyboard shortcut" list. A soundpack's binding, or the player's own hotkey, on the same key wins over genericMud's, so pick keys popular packs don't use. Shift with anything but a function key is never a shortcut: Shift is for capitals and for selecting text.
10. genericMud has two windows: the native wx one, and the web one (`--web`, in `frontend/`). Change both, or say in the pull request and the README which one your change covers.

## Anything the MUD sends is untrusted

- Text from the MUD must never turn into a genericMud command. That includes MXP and Pueblo links, menus and prompts, and GMCP and MSDP data. Text that the MUD puts into the command box may not start with the client prefix `/`, may not contain the world's command separator (`;` unless the world changes it), and may not contain control characters, and its length is capped.
- Parsers cap their work: sizes, counts, nesting depth, and anything else the server controls. Never compute something whose size the server chooses, like a huge exponent or an unbounded repeat, without a limit.
- Don't send a plain web address to the MUD as a command, and only download what a protocol asks for, like GMCP media, through the existing download checks.

## Threads

The engine runs on an asyncio loop in a background thread, and the wx window runs on the main thread. Never touch wx from the loop thread: hand the call over with `wx.CallAfter`. Long work, like a download, runs off the UI thread.

## Tests

- Every fix comes with a test that fails without the fix. Check that it does, and say so in the pull request.
- Test what the player gets: the words spoken, where focus lands, what's in the command box. Not just internal state.
- Give every async wait in a test a timeout, so a regression fails the test instead of hanging the whole suite.
- Undo anything global a test changes, like keyboard layouts, environment variables, or files outside `tmp_path`.

## Docs and help

- Any change a player can notice updates the README, in the section where a player would look for it, and the in-app help in `genericmud/help_text.py`. A new key also goes in the README's "Every keyboard shortcut" list and in `KEYBOARD_SHORTCUTS` in `help_text.py`.
- The in-app help is plain text, read line by line in a text box: one fact per line, no markdown.
- Write for players: what to press, and what they'll hear.
- Leave `CHANGELOG.md` alone. Release notes are written when a release is made, and they credit contributors by name.

## The pull request

- Title: one plain sentence saying what changes for a player, like "A saved world can be deleted from the world list".
- Description: what was wrong or missing; what a player does and hears now; how you tested it, including the test that failed before your fix; and anything you couldn't test, like a platform or a screen reader you don't have. The pull request template asks for these.

## Licensing

genericMud is MIT licensed. Only bring in code under a licence that allows that, and say where it came from. Ideas from other MUD clients are welcome. Code, tests or data from GPL-licensed clients, like Mudlet, or from copyrighted manuals, like VIPMud's, are not.
