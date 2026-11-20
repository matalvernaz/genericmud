"""MUSHclient's window calls: the clipboard, the command box, notepads and the browser.

Channel history plugins (Chatpack, Erion, Miriani-Next) copy messages to the clipboard and
paste them into the command box; Mush-Z keeps its channel log in a notepad; many packs open
their help pages in the browser. A window does these; without one they still answer sensibly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink


@dataclass
class _Window:
    """A window that records what packs asked of it."""

    clipboard: str = ""
    command: str = ""
    selection: tuple[int, int] = (0, 0)
    opened: list[str] = field(default_factory=list)
    notepads: list[tuple[str, str]] = field(default_factory=list)

    def get_clipboard(self) -> str:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self.clipboard = text

    def get_command(self) -> str:
        return self.command

    def set_command(self, text: str) -> bool:
        if self.command:
            return False
        self.command = text
        return True

    def paste_command(self, text: str) -> str:
        start, end = self.selection
        replaced = self.command[start:end]
        self.command = self.command[:start] + text + self.command[end:]
        self.selection = (start + len(text), start + len(text))
        return replaced

    def select_command(self, first: int, last: int) -> None:
        end = len(self.command) if last < 0 else min(last, len(self.command))
        self.selection = (max(0, first - 1), end)

    def open_url(self, url: str, *, announced: bool = False) -> bool:
        self.opened.append(url)
        return True

    def show_notepad(self, title: str, text: str) -> None:
        self.notepads.append((title, text))


class _WindowedSink(RecordingSink):
    def __init__(self, window: _Window | None) -> None:
        super().__init__()
        self.window = window

    def pack_window(self):
        return self.window


def _pack(script: str = "", rules: str = "", *, window: _Window | None = None):
    sink = _WindowedSink(window)
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><plugin name='p' id='aaaaaaaaaaaaaaaaaaaaaaaa'/>"
        f"<script><![CDATA[{script}]]></script>{rules}</muclient>"
    )
    return sink, engine, pack


def test_the_clipboard_goes_to_the_window():
    window = _Window(clipboard="from the player")
    sink, _engine, _pack_ = _pack(
        "Send(GetClipboard()); SetClipboard('a channel message'); Send(GetClipboard())",
        window=window,
    )
    assert sink.sent == ["from the player", "a channel message"]
    assert window.clipboard == "a channel message"


def test_without_a_window_the_clipboard_still_round_trips():
    sink, _engine, _pack_ = _pack("SetClipboard('kept'); Send(GetClipboard())")
    assert sink.sent == ["kept"]


def test_the_command_box_like_channel_histories_use_it():
    # channel_history.xml clears the box with SetCommandSelection(1, #GetCommand()) and
    # PasteCommand(""), then pastes the message the player picked.
    window = _Window(command="old words")
    sink, _engine, _pack_ = _pack(
        "Send(tostring(SetCommand('ignored')))\n"
        "SetCommandSelection(1, #GetCommand())\n"
        "Send(PasteCommand(''))\n"
        "Send('[' .. GetCommand() .. ']')\n"
        "Send(PasteCommand('Bob says hi'))\n"
        "Send(GetCommand())\n"
        "SetCommandSelection(1, -1); PasteCommand('')\n"
        "Send(tostring(SetCommand('reply')))\n"
        "Send(GetCommand())\n",
        window=window,
    )
    assert sink.sent == [
        "30020", "old words", "[]", "", "Bob says hi", "0", "reply",
    ]


def test_without_a_window_the_command_box_is_kept_in_memory():
    sink, _engine, _pack_ = _pack(
        "PasteCommand('north'); Send(GetCommand()); Send(tostring(SetCommand('south')))"
    )
    assert sink.sent == ["north", "30020"]


def test_open_browser_opens_web_and_mail_links_only():
    window = _Window()
    sink, _engine, _pack_ = _pack(
        "Send(tostring(OpenBrowser('https://example.com/help')))\n"
        "Send(tostring(OpenBrowser('mailto:someone@example.com')))\n"
        "Send(tostring(OpenBrowser('file:///etc/passwd')))\n"
        "Send(tostring(OpenBrowser('calc.exe')))\n"
        "Send(tostring(OpenBrowser('')))\n"
        "Send(tostring(utils.shellexecute('http://example.com')))\n"
        "local ok, why = utils.shellexecute('notepad.exe'); Send(tostring(ok))\n",
        window=window,
    )
    assert sink.sent == ["0", "0", "30046", "30046", "30046", "true", "nil"]
    assert window.opened == [
        "https://example.com/help", "mailto:someone@example.com", "http://example.com",
    ]


def test_notepads_collect_text_and_show_only_when_asked():
    # Mush-Z appends every channel message to its "comlog" notepad: appending must never
    # put a window in front of the player, only ActivateNotepad does.
    window = _Window()
    sink, _engine, _pack_ = _pack(
        "AppendToNotepad('comlog', 'Bob: hi', '\\r\\n')\n"
        "AppendToNotepad('comlog', 'Ann: hello', '\\r\\n')\n"
        "Send(GetNotepadText('comlog'))\n"
        "Send(tostring(GetNotepadLength('comlog')))\n"
        "Send(table.concat(GetNotepadList(), ','))\n"
        "Send(tostring(ActivateNotepad('missing')))\n"
        "Send(tostring(ActivateNotepad('comlog')))\n"
        "ReplaceNotepad('comlog', 'fresh')\n"
        "Send(GetNotepadText('comlog'))\n"
        "SendToNotepad('help', 'line one')\n"
        "Send(tostring(NotepadReadOnly('help', true)))\n"
        "Send(tostring(CloseNotepad('help')))\n"
        "Send(tostring(GetNotepadText('help')))\n"
        "utils.appendtonotepad('utils', 'a')\n"
        "utils.appendtonotepad('utils', 'b', true)\n"
        "Send(GetNotepadText('utils'))\n",
        window=window,
    )
    assert sink.sent == [
        "Bob: hi\r\nAnn: hello\r\n", "21", "comlog", "false", "true", "fresh", "1", "1",
        "", "b",
    ]
    assert window.notepads == [("comlog", "Bob: hi\r\nAnn: hello\r\n")]


def test_notepad_titles_match_whatever_their_case():
    # MUSHclient's FindNotepad compares titles ignoring case. Toastush fills "Changelog" and
    # then opens "changelog", so its changelog never came up.
    window = _Window()
    sink, _engine, _pack_ = _pack(
        "AppendToNotepad('Changelog', 'v1', '\\r\\n')\n"
        "AppendToNotepad('CHANGELOG', 'v2', '\\r\\n')\n"
        "Send(tostring(ActivateNotepad('changelog')))\n"
        "Send(GetNotepadText('changelog'))\n"
        "Send(tostring(GetNotepadLength('changeLOG')))\n"
        "Send(table.concat(GetNotepadList(), ','))\n"
        "Send(tostring(NotepadReadOnly('changelog', true)))\n"
        "Send(tostring(CloseNotepad('changelog')))\n",
        window=window,
    )
    assert sink.sent == ["true", "v1\r\nv2\r\n", "8", "Changelog", "1", "1"]
    assert window.notepads == [("Changelog", "v1\r\nv2\r\n")]


def test_a_trigger_can_send_to_a_notepad():
    sink, engine, pack = _pack(
        rules="<triggers><trigger name='log' match='^(.+) tells you (.+)$' regexp='y' "
        "enabled='y' send_to='5'><send>%1: %2</send></trigger></triggers>",
    )
    pack.dispatch_line("Bob tells you hi")
    engine.process_line(Line("Bob tells you hi"))
    assert pack._notepads == {"Trigger: log": "Bob: hi\r\n"}
    assert sink.sent == []


def test_a_notepad_keeps_its_newest_text_when_it_grows_too_long():
    sink, _engine, pack = _pack(
        "for i = 1, 3000 do AppendToNotepad('big', string.rep('x', 499), '\\n') end\n"
        "AppendToNotepad('big', 'the end')\n"
    )
    text = pack._notepads["big"]
    assert len(text) <= 1_000_000
    assert text.endswith("the end")
