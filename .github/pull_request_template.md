<!--
Title: one plain sentence saying what changes for a player, like "A saved world can be deleted from the world list".
If this builds on another open pull request, start with "Stacked on #N".
The rules behind the checklist are in CONTRIBUTING.md.
-->

## What was wrong or missing


## What a player does and hears now

<!-- The exact words spoken, where focus goes, which keys. Say which window: the native one, the web one (--web), or both. -->


## How you tested it

<!-- The test that failed before your fix. Which platforms, and which screen reader if you used one. Anything you couldn't test. -->


## Checklist

- [ ] Nothing fails silently: every new key, command or button works or says why not, and a failure says its reason.
- [ ] Text the player typed is never thrown away.
- [ ] Every new control has a label and a shortcut key, and focus goes back where the player was.
- [ ] Text from the MUD can't become a genericMud command, and parsers cap their work.
- [ ] A test fails without the fix (run against the commit before it, failure shown above), every async wait in a test has a timeout, and wx tests use `pytest.importorskip("wx")`.
- [ ] If this changes something existing players already have (a default, a file format, what's logged, rule order), the description says so first.
- [ ] MUD text never reaches code as source text, and anything it can put in the command box is checked line by line.
- [ ] README.md and genericmud/help_text.py are updated for anything a player can notice, and CHANGELOG.md is untouched.
- [ ] `uv run ruff check .`, `uv run pytest -q`, the `node --test` line from CONTRIBUTING.md and `node --check frontend/src/app.js` all pass.
