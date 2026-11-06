# Command input modes

The native wx window offers one global preference in **View → Command input mode**:
**Standard** (the default) and **Editable command buffer (VIPMud-style)**.
It is saved in `ui-prefs.toml` as `command_input_mode = "standard"` or `"editable"`.
The choice applies only when opening a session. Existing sessions keep their controls,
text, selection and mode; no live draft conversion takes place. The web frontend is
unchanged. The unpublished prototype's boolean preference is migrated on load.

## What the player edits

Standard keeps current genericMud behavior: a compose box, conventional sent-command
history, Enter sending its block, and Shift+Enter adding a line. The optional mode is
one native editable document: earlier commands and several unsent drafts can coexist
and be changed or removed at their original positions. It is not a list of immutable
sent commands. Each session owns a separate document.

| Key | Editable buffer behavior |
| --- | --- |
| Up/Down, Left/Right, Shift+arrows | Native caret movement and selection across existing rows. |
| Down at an unfinished final row | Append one empty tail without sending the draft. An existing empty tail is reused. |
| Enter | Execute the logical line at the lower selection boundary, then move to the tail. An older row remains, with an executed copy appended. |
| Shift+Enter | Execute the current line or explicit selection without changing text, caret or selection. A multiline selection sends its nonblank lines separately through normal dispatch. |
| Escape | Insert a blank row before the current row, preserving selected characters. In the first row, split at the range's start and request the first character position. At position zero, use the valid start of the new empty row. |
| Backspace | At a row boundary with no selection, preserve the separator (default VIPMud Line Entry Mode). Selections, Delete and Shift+Backspace remain native. |
| Ctrl+R | Complete from earlier document rows, including drafts, using a case-insensitive prefix. Repeat to cycle older matches; the typed prefix and text after the caret are preserved. A missing prefix/match is explained aloud. |
| Ctrl+Enter | Focus the current session's existing buffer tail, including from Output. |
| Ctrl+Space / Ctrl+Shift+Space | Keep genericMud's word completion from recent output, scoped to the logical line. |

On macOS, Ctrl+R and Ctrl+Enter use Command+R and Command+Enter.
Physical Control+Enter opens Cocoa's native context menu. Standard keeps its
existing wx modifier mapping. Modifier-only press/release events preserve the
completion cycle; an editing key or text change ends it.

Tab back from Output collapses the selection at the existing tail. A final unsent
draft remains active. Focus never clears text or creates a row. Typing a printable
character from Output also targets the tail, even if an old row was selected before
leaving. A native Cocoa mouse test confirms that click positioning can override the
focus handler's tail movement. Other platforms and dragging still need testing.
Explicit pack/user keys on Escape, Ctrl+R or Ctrl+Enter override these defaults.
Other shortcuts retain their genericMud meaning; this is not a replacement for
the entire VIPMud keymap. In particular, genericMud's
numpad and output review shortcuts are retained.

Ctrl+Enter changes meaning only for sessions opened in editable mode. Autoretype
continues to honor its saved setting and the engine's ordinary empty-input handling;
toggle it in Standard or bind `input:autoretype` to another key. Shift+Enter on an
empty/space-only command does not execute it.

## Enter and identical rows

Ordinary Enter updates the document independently from dispatch. Enter inside the
final row can append a copy even though it is the last logical row; Enter at the
absolute end completes that row. Empty Enter appends a separator before cleanup.

Afterward compare exactly the last two completed rows, only if there are at least
three logical CRLF delimiters. If they are identical, remove the later one once,
leaving an empty tail. Case and spaces matter. This applies equally to manually
entered, edited, and recalled text; there is no submitted-row identity. The short
buffer exception is intentional. Removing a duplicate never cancels execution.
Earlier unsent text otherwise remains at its existing location.

## Shared execution and programmatic input

All submissions use the existing engine input path: client commands, aliases,
stacking, logging, autoretype and secret dispatch keep their behavior. Disconnected
submission retains both document and selection while the existing reconnect flow
runs. Client-only selections work offline. A session that is still starting retains
its input and explains why it cannot submit yet.

Plugin command APIs address one logical line. `GetCommand` returns that line;
`SetCommand` still refuses to replace a nonempty line. `SelectCommand` uses one-based
Unicode character columns within the line, with -1 for its end. `PasteCommand`
replaces a selection within that line; a cross-line UI selection is refused and
explained rather than silently erasing several old rows. Multiline plugin insertion
creates drafts and never executes them. Enter executes only one row; multiline
execution requires an explicit selection and Shift+Enter. Standard's APIs remain
unchanged. MXP/Pueblo insertion retains the engine's untrusted-input validation and
replaces one logical row, choosing the tail when activated from Output.

A hidden-input notification suspends the document and shows a separate labelled
native password field. Passwords bypass document/history storage and completion;
unhide destroys that field and its undo buffer. An unsent password is discarded
with an announcement on unhide. The document and its selection are restored.
Standard retains its original masking behavior.

## Compatibility evidence and limits

An experienced VIPMud user reported keyboard/NVDA observations corroborating draft
preservation, Down-tail creation, in-place historical editing, old-line execution,
manual/recalled duplicate removal, Shift+Enter with/without selection, Escape, and
boundary/selection deletion. The three-delimiter exception, empty Enter, final-row
caret distinction and first-row Escape details also follow the static behavioral
specification. Those edge cases were not all separately observed in that report.
No proprietary code, decompiled implementation, manual text or binary data is included.

This first version intentionally does not emulate VIPMud's script-language brace
assembly, its Line Entry Mode Off option, all Ctrl+R whitespace/search edge cases,
ANSI/DBCS limitations, timer races, or character-based trimming. The buffer remains
in memory until its session closes; it is not saved across restarts. There is no
automatic pruning that could silently discard drafts. Long sessions may require
manual deletion. Native edits do not copy or reconcile a second Python document;
submission and prefix search scan the current text. Large-buffer latency and screen
reader response still need field testing. Lines scroll horizontally rather than
wrapping; VIPMud's exact wrapped-line behavior remains outside the compatibility claim.

Cocoa selection uses UTF-16 positions in the tested wx build while GetRange reads
code points. The optional editor converts those positions explicitly, and other
ports map through native range APIs to account for CRLF. A first-row Escape request
inside a CRLF is normalized to a valid caret rather than deliberately splitting a
native separator. Other native editing/undo details can vary by platform.

Automated genericMud checks on macOS use real wxPython 4.2.5 / wxWidgets 3.2.9 Cocoa
controls and focus, with synthetic key/character/submission events and deterministic
native caret/selection/deletion operations. They cover the confirmed scenarios,
Unicode/non-BMP and combining marks, completion, plugins on the loop thread, password
isolation, session/mode isolation, large documents, and Standard regressions.
A separate macOS review used the desktop driver's genuine OS-delivered keys and a
local recording fake engine. It verified Enter/Shift+Enter dispatch counts, arrows
and selection, Down, Escape, boundary/selection deletion, Output focus/typing,
Command+R cycling, Command+Enter, Standard regressions, undo/redo, clipboard and
mouse positioning. Unicode clipboard paste and subsequent dispatch passed; the
typing path did not deliver the non-ASCII characters to wx, so Unicode keyboard
input and IME remain unverified. No screen reader was used. See
[native input review](command-entry-native-review.md) for fixtures, evidence and limits.

An optional foreground smoke test sends keys with wx.UIActionSimulator:
`GENERICMUD_TEST_OS_INPUT=1 uv run pytest -q -rs tests/test_command_entry_os_input.py`.
It is disabled by default and skipped whenever CI or GITHUB_ACTIONS is set, even
with an inherited opt-in flag. Permission and plain-control input checks prevent
missing OS input from looking like a pass.
This environment denies event posting by Python, so that particular driver remains
unverified here; the desktop driver provided the successful OS-input coverage.

These checks do not establish hardware keyboard/IME delivery, actual VoiceOver
speech, Windows/NVDA/JAWS or GTK/Orca accessibility. VIPMud/NVDA observations do not
validate genericMud's accessibility. Manual testing of that matrix, other platforms'
focus/click ordering, announcement timing and plugin integrations remains required.
