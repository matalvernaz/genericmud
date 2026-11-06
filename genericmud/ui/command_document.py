"""Submission policy for an editable command document, independent of wx positions.

The native editor owns editing/undo/selection. Known-submitted lines are only a
hint for avoiding recalled copies. Any native edit forgets all hints, including
identical-text replacements: native notifications do not expose a reliable edit
range/identity for every paste, undo, drag/drop and context-menu action.
"""

from __future__ import annotations


class CommandDocument:
    def __init__(self) -> None:
        self.text = ""
        self._submitted: set[int] = set()

    def sync(self, text: str, *, edited: bool = False) -> None:
        if edited or text != self.text:
            self._submitted.clear()
            self.text = text

    def complete(self, line: int) -> str:
        """Record Enter, returning only the text to append to the native editor.

        Always execute separately, even when the returned suffix is empty. An empty
        command remains empty so the engine's existing paging/autoretype path owns it.
        """
        lines = self.text.split("\n")
        command = lines[line]
        if not command:
            return ""
        newest = len(lines) - 2
        recalled_duplicate = (
            lines[-1] == ""
            and line in self._submitted
            and newest in self._submitted
            and lines[newest] == command
        )
        self._submitted.add(line)
        if line == len(lines) - 1:
            suffix = "\n"
        elif recalled_duplicate:
            suffix = ""
        else:
            appended = len(lines) - 1 if lines[-1] == "" else len(lines)
            suffix = ("" if lines[-1] == "" else "\n") + command + "\n"
            self._submitted.add(appended)
        self.text += suffix
        return suffix
