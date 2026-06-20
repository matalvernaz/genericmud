"""Resolve bundled data files in both source and PyInstaller-frozen runs."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """Directory under which ``frontend/`` and the package data live.

    PyInstaller extracts bundled data to ``sys._MEIPASS``; from source it's the
    project root (the parent of the ``genericmud`` package).
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled is not None:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent
