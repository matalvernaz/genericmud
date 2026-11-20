"""The cross-platform PyInstaller driver produces correct, accessible flags per platform."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.build_app import pyinstaller_args


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_every_platform_builds_windowed_never_console(platform):
    # A console window steals screen-reader focus; the GUI subsystem is required everywhere.
    args = pyinstaller_args(platform)
    assert "--windowed" in args
    assert "--console" not in args
    assert args[-1] == "run_genericmud.py"
    assert "--copy-metadata" in args  # so the updater can read the frozen version


def test_add_data_separator_is_host_specific():
    win = pyinstaller_args("win32")
    posix = pyinstaller_args("darwin")
    assert "frontend;frontend" in win  # Windows wants ';'
    assert "frontend:frontend" in posix  # POSIX wants ':'


def test_windows_only_hidden_imports_stay_off_mac_and_linux():
    win = pyinstaller_args("win32")
    assert "win32com.client" in win and "pythoncom" in win
    for platform in ("darwin", "linux"):
        args = pyinstaller_args(platform)
        assert "win32com.client" not in args
        assert "pythoncom" not in args


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_every_platform_freezes_the_prism_speech_library(platform):
    # prism speaks through a native library the wheel ships (prism.dll / libprism) plus a
    # cffi extension. Drop the collect and the app still builds -- it just can't talk, which
    # for a self-voicing client is a silent brick. Guard the flag on all three platforms.
    args = pyinstaller_args(platform)
    assert args[args.index("prism") - 1] == "--collect-all"


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_every_platform_registers_the_prism_hook(platform):
    # --collect-all misses the cffi bridge in prism/_native/ on every platform, so without the
    # hook the frozen app raises on `import prism` and falls back to the system voice silently.
    args = pyinstaller_args(platform)
    assert args[args.index("hooks") - 1] == "--additional-hooks-dir"


def test_macos_build_sets_a_bundle_identifier():
    args = pyinstaller_args("darwin")
    assert "--osx-bundle-identifier" in args


def test_bass_goes_in_the_apps_bass_folder(tmp_path):
    folder = tmp_path / "bass"
    folder.mkdir()
    for name in ("bass.dll", "bass_fx.dll", "bass.txt"):
        (folder / name).write_bytes(b"x")
    win = pyinstaller_args("win32", folder)
    assert win[win.index(f"{folder / 'bass.dll'};bass") - 1] == "--add-binary"
    assert win[win.index(f"{folder / 'bass.txt'};bass") - 1] == "--add-data"
    assert win[-1] == "run_genericmud.py"
    assert f"{folder / 'bass_fx.dll'}:bass" in pyinstaller_args("linux", folder)
    assert not any("bass" in arg for arg in pyinstaller_args("linux"))


def test_the_licence_notices_go_in_the_app(tmp_path):
    notices = tmp_path / "THIRD-PARTY-NOTICES.txt"
    notices.write_text("x", encoding="utf-8")
    win = pyinstaller_args("win32", notices=notices)
    assert win[win.index(f"{notices};.") - 1] == "--add-data"
    assert f"{notices}:." in pyinstaller_args("darwin", notices=notices)
    assert win[-1] == "run_genericmud.py"


def test_the_build_lists_its_contents_folder_last(tmp_path):
    from genericmud.update.upgrade_manager import MANIFEST_NAME
    from tools.build_app import list_contents

    contents = tmp_path / "dist" / "genericMud" / "_internal"
    (contents / "wx").mkdir(parents=True)
    (contents / "wx" / "core.pyd").write_bytes(b"x")
    list_contents(contents)
    listed = (contents / MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert listed[1:] == ["wx/core.pyd", MANIFEST_NAME]
    list_contents(tmp_path / "nowhere")  # a build with no contents folder (the macOS .app)
    assert not (tmp_path / "nowhere").exists()


def test_the_windows_build_leaves_out_what_the_app_never_runs():
    # pygame came in whole: its docs, tests and examples, and through its tests Python's
    # test package and Tcl/Tk. Pythonwin's MFC windows came through win32com's makepy.
    args = pyinstaller_args("win32")
    assert "pygame" not in [args[i + 1] for i, a in enumerate(args) if a == "--collect-all"]
    excluded = {args[i + 1] for i, a in enumerate(args) if a == "--exclude-module"}
    assert {"pygame.tests", "pygame.examples", "tkinter", "test", "pywin", "win32ui"} <= excluded
    assert "win32com.client" in args  # the SAPI voice itself stays


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_mac_and_linux_builds_still_take_pygame_whole(platform):
    args = pyinstaller_args(platform)
    assert args[args.index("pygame") - 1] == "--collect-all"
    assert "--exclude-module" not in args


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
def test_every_build_freezes_without_the_test_tools(platform):
    # PyInstaller takes in any installed package a bundled library might import, so the test
    # tools (and trustme's cryptography) must be gone between the tests and the freeze, and
    # the freeze must not sync them back in.
    root = Path(__file__).resolve().parents[1]
    workflow = (root / f".github/workflows/build-{platform}.yml").read_text(encoding="utf-8")
    tests = workflow.index("pytest -q")
    drop = workflow.index("uv sync --all-extras --locked --no-dev")
    freeze = workflow.index("tools/build_app.py\n")
    assert tests < drop < freeze
    freeze_line = workflow[workflow.rindex("\n", 0, freeze) : freeze]
    assert "uv run --no-sync python" in freeze_line
