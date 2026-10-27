"""Build-blind checks for the web renderer's links.

There is no JavaScript test harness in this project (the frontend is plain ES modules with
no bundler), so the renderer's contract is pinned by reading its source the same way
``test_native_ui_source.py`` pins the native window's.
"""

from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend/src"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_the_output_draws_real_links_from_offsets():
    source = _read("output.js")
    # A real anchor, so the browser treats it as a link: focusable, announced as one, and
    # activated by Enter.
    assert 'document.createElement("a")' in source
    assert 'anchor.className = "mxp-link"' in source
    assert 'anchor.href = "#"' in source
    # Built from the offsets the engine sent -- no markup is parsed in the renderer.
    assert "text.slice(start, end)" in source


def test_a_drawn_link_carries_its_command_as_data():
    # The command must not come from the anchor's own visible text, so a rewritten line cannot
    # change what gets sent.
    source = _read("output.js")
    assert "anchor.dataset.command = command" in source
    assert "anchor.dataset.words" in source


def test_activating_a_link_does_not_navigate_the_page():
    source = _read("output.js")
    assert "event.preventDefault()" in source


def test_activating_a_link_reports_the_click_to_the_engine():
    # The renderer decides nothing: the engine owns the policy for what a link may do.
    app = _read("app.js")
    assert 'type: "link"' in app
    assert "new Output(outputEl," in app


def test_a_web_link_is_opened_as_a_new_tab_without_a_handle_back():
    source = _read("app.js")
    assert 'open_url: (m) => window.open(m.url, "_blank", "noopener,noreferrer")' in source


def test_links_are_visibly_and_focusably_distinct():
    source = _read("style.css")
    assert "#output a.mxp-link:focus" in source
    assert "outline" in source
