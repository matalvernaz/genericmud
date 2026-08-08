"""The cross-platform PyInstaller driver produces correct, accessible flags per platform."""

from __future__ import annotations

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


def test_macos_build_sets_a_bundle_identifier():
    args = pyinstaller_args("darwin")
    assert "--osx-bundle-identifier" in args
