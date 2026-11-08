# Instructions for AI coding assistants

Codex and other coding agents read this file automatically; Claude Code reads it through `CLAUDE.md`. The full rules are in `CONTRIBUTING.md`: read all of it before changing anything. These are the ones review catches most often.

- genericMud's players are blind or low-vision, and most use a screen reader. Judge every change by what they hear and whether they can reach it from the keyboard.
- Nothing fails silently. Every key, command and button either works or says why not, and a failure says its reason: "Connect failed: host not found", not "Connection unavailable".
- Never throw away text the player typed.
- Say things once, and put focus back where the player was.
- Every wx control has a name: a `StaticText` created immediately before it, plus `SetName`. Never use `wx.PD_AUTO_HIDE`.
- Never touch wx from the asyncio loop thread; use `wx.CallAfter`.
- Text from the MUD is untrusted and must never become a genericMud command: no leading `/`, no command separator, no control characters, and a capped length, checked line by line. Cap every parser's work.
- Never paste MUD text into code. Lua a rule runs gets the match as values (`line`, `wildcards`), not as source text.
- Every fix has a test that fails without it: run it against the commit before your fix. Test the path the app really takes, with real wx controls under `xvfb-run` where the player's view or focus matters. Every async wait in a test has a timeout. wx tests start with `pytest.importorskip("wx")`.
- Don't change what existing players already have (a default, a file format, what's logged, rule order, the Lua version) without saying so first in the pull request.
- Answering review: fixes go in as new commits on top, pushed once per round, and you reply once on the top pull request. Rebasing a whole stack per fix emails the maintainer once per pull request.
- Update `README.md` and `genericmud/help_text.py` for anything a player can notice. In a contribution, leave `CHANGELOG.md` alone: release notes are written at release time.
- One change per pull request, titled as one plain sentence about what changes for a player. Fill in the pull request template.
- Before you finish, run `uv run ruff check .`, `uv run pytest -q` (not piped), the `node --test` line from `CONTRIBUTING.md`, and `node --check frontend/src/app.js`.

## Where things live

- Shared session: `genericmud/app/engine_app.py`, with methods grouped by job in sibling mixins.
- Native window: `genericmud/ui/main_window.py` and `session_panel.py`; `wx_app.py` starts them.
- Web window: `genericmud/web_launcher.py`, `genericmud/bridge/` and `frontend/`.
- Follow the [full code map in CONTRIBUTING.md](CONTRIBUTING.md#where-things-live) for protocols,
  automation, scripting, soundpacks, speech, audio, settings, tests and packaging.
- Patch a name in the module that reads it. Use `tests/ui_source.py` for native source assertions.
