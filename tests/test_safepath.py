"""Path-safety helpers and the pack/MSP sound-path confinement they back."""

from __future__ import annotations

import os

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.safepath import (
    confine,
    is_unsafe,
    resolve_media,
    sanitize_component,
    within,
)
from genericmud.scripting.api import ScriptApi


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",  # POSIX absolute
        "\\\\host\\share\\x.wav",  # Windows UNC (NTLM-leak vector)
        "//host/share/x.wav",  # forward-slash UNC
        "C:/Windows/x.wav",  # drive-qualified
        "C:x.wav",  # drive-relative
        "../secret.wav",  # parent traversal
        "sub/../../secret.wav",  # traversal mid-path
        "a\\..\\..\\b",  # backslash traversal
        "",  # empty
        "a\x00b",  # NUL
    ],
)
def test_is_unsafe_true(name):
    assert is_unsafe(name) is True


@pytest.mark.parametrize("name", ["ok.wav", "combat/hit.wav", "a/b/c.ogg", "file.with.dots.wav"])
def test_is_unsafe_false(name):
    assert is_unsafe(name) is False


def test_confine_keeps_inside(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.wav").write_bytes(b"x")
    got = confine(tmp_path, "sub/a.wav")
    assert got == (tmp_path / "sub" / "a.wav").resolve()


def test_confine_rejects_escape(tmp_path):
    assert confine(tmp_path, "../x") is None
    assert confine(tmp_path, "/etc/passwd") is None
    assert confine(tmp_path, "\\\\host\\share\\x") is None


def test_within(tmp_path):
    inside = tmp_path / "a" / "b.wav"
    assert within(tmp_path, inside) is True
    assert within(tmp_path, tmp_path.parent / "other.wav") is False


def test_resolve_media_finds_under_root_by_path_and_basename(tmp_path):
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "hit.wav").write_bytes(b"x")
    root = str(tmp_path / "sounds")
    want = str((tmp_path / "sounds" / "hit.wav").resolve())
    assert resolve_media("hit.wav", root) == want
    # a hard-coded subpath still resolves by basename under the user's sounds folder
    assert resolve_media("packdir/hit.wav", root) == want


def test_resolve_media_rejects_unsafe_and_missing(tmp_path):
    root = str(tmp_path)
    assert resolve_media("/etc/passwd", root) is None
    assert resolve_media("\\\\host\\share\\x.wav", root) is None
    assert resolve_media("../secret.wav", root) is None
    assert resolve_media("nope.wav", root) is None
    assert resolve_media("x.wav", None) is None  # no root configured


def test_sanitize_component():
    assert sanitize_component("Star Conquest") == "Star_Conquest"
    assert sanitize_component("../../etc/passwd") == "etc_passwd"
    assert sanitize_component("...") == "session"
    assert sanitize_component("", fallback="x") == "x"


# --- ScriptApi._resolve: the pack-side sound path (finding A) ---


def _api(base: str) -> ScriptApi:
    return ScriptApi(AutomationEngine(), base_dir=base)


def test_scriptapi_resolve_relative_ok(tmp_path):
    (tmp_path / "hit.wav").write_bytes(b"x")
    api = _api(str(tmp_path))
    assert api._resolve("hit.wav")[0] == os.path.join(str(tmp_path), "hit.wav")


def test_scriptapi_resolve_blocks_absolute_and_unc(tmp_path):
    api = _api(str(tmp_path))
    assert api._resolve("/etc/passwd")[0] == ""
    assert api._resolve("\\\\attacker\\share\\x.wav")[0] == ""


def test_scriptapi_resolve_blocks_traversal(tmp_path):
    # A file that DOES exist just outside the pack dir must not be reachable via ../.
    outside = tmp_path.parent / "secret.wav"
    outside.write_bytes(b"x")
    api = _api(str(tmp_path / "pack"))
    (tmp_path / "pack").mkdir()
    assert api._resolve("../secret.wav")[0] == ""


def test_scriptapi_resolve_unsafe_falls_back_to_sounds_dir(tmp_path):
    # An unsafe reference still resolves by basename under the configured Sounds folder.
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "hit.wav").write_bytes(b"x")
    engine = AutomationEngine()
    engine.set_var("sppath", str(sounds))
    api = ScriptApi(engine, base_dir=str(tmp_path / "pack"))
    (tmp_path / "pack").mkdir()
    assert api._resolve("C:/anywhere/hit.wav")[0] == str(sounds / "hit.wav")
