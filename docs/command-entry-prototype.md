# Editable command entry: native feasibility prototype

This is an independent prototype for review, based on upstream main
`f76764605cd40dda8684b24c70d0ea895708b68a`. It is not an approved upstream
contribution. No VIPMud source, decompiled implementation, or machine code is used.

## Trying it

Run the native client from this checkout using its usual environment:

```sh
.venv/bin/python run_genericmud.py
```

In View, enable **Editable command buffer (new sessions)**, then open a new
session. The global boolean `editable_command_buffer` is persisted in
`ui-prefs.toml`, and defaults to false. Existing sessions retain their original
mode, drafts and selections. The web frontend continues to use normal input.

Up/Down, Left/Right, Shift+arrows, Backspace/Delete, clipboard and undo/redo belong
to the native editor. Lines do not wrap; long commands scroll horizontally.
**Shift+Enter inserts an unsent newline. Enter executes one logical line.**
When a range is selected, Enter uses the line at the start of that range, not the
selected text. It then collapses the selection at the document's end.

To park a draft and keep entering commands, use Shift+Enter after the draft:

```text
north
look target
south
east

```

Executing `south` and `east` does not move `look target`. Edit it in place, or
press Enter there to execute it and append its copy after the later commands.
Returning from Output moves to the existing tail: an empty line or the end of a
final draft. Focus never deletes text or adds a newline. Typing a printable
character from Output goes to that tail, regardless of an earlier selection.

## Architecture decision

A native multiline `wx.TextCtrl` supplies the document editing and accessible
text surface. A single-line field with extra history state would require custom
cross-line navigation, selection, deletion, clipboard and undo behavior. A
Scintilla/custom editor or separate history editor would add a larger surface
with less-established accessibility for this client. The optional native control
plus a small independent submission policy is the smallest useful vertical slice.

`SessionPanel` still sends `{type: input, text: current_line}` through the existing
EngineApp route, including connection checks, client commands, aliases, stacking,
secret dispatch and autoretype. Empty Enter sends empty input without adding a
row; EngineApp owns paging and autoretype. `_history` remains a separate record of
unmasked typed submissions (with repeats). The document's arrows never restore
text from that sent-only list. Compass/macro submissions keep their existing route
and do not insert rows in the document.

A server's hidden-input notification suspends the optional editor and exposes a
transient native single-line `TE_PASSWORD` control. Its submissions bypass document
and history storage. Unhide destroys the secret control and its undo buffer;
the existing document is restored. Normal mode keeps its original masking/history
behavior; this prototype does not redesign it.

Programmatic get/set/select command APIs address the current logical line in the
optional editor. Set still refuses a nonempty line. Explicit selection/paste may
span rows; MXP input replaces one line, preserving the rest of the document. A
link activated from Output targets the tail first. Completion replaces only the
current logical line and preserves the surrounding document.

Cocoa wx 3.2.9 uses UTF-16 selection coordinates while GetRange reads code-point
coordinates for non-BMP text in this environment. New editor reads therefore use
an explicit Cocoa UTF-16 range adapter. Other ports translate Python text offsets
through native GetRange. All native insertions/deletions use native positions;
normal-mode position handling is unchanged. Windows CRLF and GTK behavior still
require execution on those platforms.

## Recalled copies and manual repetitions

After a submission, the policy remembers the original submitted row and any
appended copy. Without an intervening edit, re-executing a known submitted line
whose text equals the completed line immediately before an empty tail does not
append an adjacent duplicate. It still executes. Typing the same command into the
new final line and submitting it retains both rows.

**Any native text edit forgets all origin hints**, including replacements that
leave the same text. Native text notifications do not reliably identify the full
edit range for paste, undo, context-menu actions or drag/drop. Keeping guessed
line identities could incorrectly suppress a manually retyped command. The
conservative policy permits an extra recalled copy after an edit elsewhere. It
never removes existing rows or suppresses a new final line merely for matching.
Stable provenance through arbitrary edits is follow-up work, not claimed here.

## Acceptance coverage

`tests/test_command_document.py` tests submission policy without wx.
`tests/test_command_entry.py` runs real SessionPanel controls in an isolated native
process with no connection startup. It uses real focus events and synthetic wx
character/key/submission events. Cocoa also exercises native CHAR-to-TEXT_ENTER
translation and Tab navigation. Native caret/selection/deletion APIs are used
deterministically; physical arrow/deletion key delivery is not established.

| Scenario | Prototype behavior and test evidence |
| --- | --- |
| 1. Draft between later commands | Preserved in place; policy and native probe. |
| 2. Revisit/edit draft | Up/Down handlers pass through; native caret is placed on the draft and editing preserves its location. Physical Up remains a manual check. |
| 3. Output to final empty line | Real native focus/Tab return collapses the selection at the existing empty tail. |
| 4. Output to final draft | Same transition selects the end of the existing draft without adding text. |
| 5. Edit older command | Native replacement changes its original row without moving it. |
| 6. Execute older command | Original and intervening drafts remain; executed copy and empty tail append. |
| 7. Repeat newest history | Executes again with no copy while provenance remains known. Editing invalidates that hint. |
| 8. Manual identical repetitions | Both completed rows remain. |
| 9. Cross-line selection/deletion | Native selection and Remove join surviving text fragments; Shift/arrows/deletion key handlers pass through. Physical gestures remain a manual check. |
| 10. Session isolation | Real Simplebook page switching retains distinct document text. |
| 11. Password/normal input | Separate password control; secrets excluded from document/history; normal clear/submit and parked-draft history still work. Engine alias/stacking/client-command/autoretype/secret integration is exercised. |
| 12. Typing from Output | Real focus plus synthetic printable input appends to empty/draft tail, never overwrites an old selected row. |

Additional checks include Cyrillic, emoji, combining marks and CJK; plugin get/set/
select/paste; Unicode completion; Enter with a cross-line selection; disconnected
submission retaining text and selection; and repeated mask/unmask notifications.

## Deliberate limits and work before a PR

- Shift+Enter does not implement VIPMud's execute-without-append or execute-selection
  behavior; it creates draft lines. A future explicit execution action could reuse
  the existing dispatch route without mutating the document. No proprietary script
  brace assembly or historical edge cases are copied.
- No live mode conversion, cross-restart document storage, or normal-history seeding.
  Changing the setting affects new sessions only; closing a session discards drafts
  as it does today. Production mode migration needs explicit draft preservation.
- No automatic buffer pruning: losing an old unsent draft would be unacceptable.
  Policy synchronization and native range mapping scan document text; large-buffer
  performance, accessibility latency and an explicit retention/archive policy need
  measurement and design before upstreaming.
- No web implementation. A web version needs a textarea/document design, its own
  UTF-16 selection and composition-aware event handling, per-session storage,
  password fallback and browser/assistive-technology tests. wx code cannot supply it.
- No Windows/NVDA/JAWS or GTK/Orca testing. Native macOS control assertions do not
  establish VoiceOver speech or IME composition behavior. Verify announcement order,
  mouse focus/hit-testing, drag selection, physical editing/undo/clipboard gestures,
  Tab/Shift+Tab, IME confirmation, AltGr, plugin interactions and long commands on
  all supported platforms. Native mouse hit-testing may override focus-time caret
  movement; keyboard return is the tested contract.

Before an upstream PR, agree on Shift+Enter and plugin column semantics, improve
origin tracking if required, choose a retention policy, run that accessibility/
platform matrix, and obtain user evaluation of the editing model.

## Verification on 2026-11-06

Host: macOS Cocoa, Python 3.12.14, wxPython 4.2.5 / wxWidgets 3.2.9.

- `.venv/bin/ruff check .`: passed.
- `.venv/bin/pytest -q -ra`: **2228 passed, 106 skipped** (16.36 seconds).
  The new native probe ran successfully. Existing skips include macOS-guarded
  dialog/pack-window fixtures, unavailable BASS/pygame/plugin fixtures, and
  Windows-only tests. No skipped test is represented as an executed check.
- `node --test tests/test_frontend_*.mjs`: **30 passed**, using bundled Node 24.22.1.
- `node --check frontend/src/app.js`: passed.
- `git diff --check`: passed.

Changed files: `genericmud/ui/command_document.py` (submission policy),
`genericmud/ui/command_entry.py` (native editor and coordinate adapter),
`genericmud/ui/wx_app.py` (optional integration), `genericmud/config/ui_prefs.py`
(persisted opt-in), `tests/test_command_document.py`, `tests/test_command_entry.py`,
`tests/test_ui_prefs.py`, and this document.
