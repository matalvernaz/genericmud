# Native input review, 6 November 2026

This records the native input review of Draft PR #74 at `b59a9b1` and the
follow-up fix for the completion issue found below. It does not certify accessibility or a release.

## Environment and actual input mechanism

macOS 27.2 arm64; Python 3.12.14; wxPython 4.2.5 / wxWidgets 3.2.9 Cocoa.
A real wx window containing the actual editable and Standard SessionPanels ran
its native event loop. Session startup coroutines were closed before any network
work. A local fake engine recorded every `on_ws_message` call, with separate
input and key-message counts. No MUD connection or game command was sent.

Fixture setup assigned initial text/selection/focus. Every tested key after that
came from the desktop automation driver's native `pressKey` operation; typing
ASCII used individual key presses. Mouse positioning used a native coordinate
click. Clipboard operations used native shortcuts; Unicode paste used the native
pasteboard. No action was delivered by calling a product handler, constructing a
wx.KeyEvent, using AX SetValue or setting the caret as a substitute for navigation.
Read-only wx event observers and state snapshots recorded the resulting text,
selection, focus, key-down/character/key-up/TEXT_ENTER events and all dispatch calls.
Assertions followed observed native state; bounded fixture acknowledgements kept
setup separate from keyboard delivery.

The first wx.UIActionSimulator attempt did **not** work. The wx frame was active,
its text control focused, and the process was the foreground application.
Simulator.Char returned true, but the control received **zero** key/character
callbacks and no text. The unsandboxed Python process reported:

- AXIsProcessTrusted: false.
- CGPreflightPostEventAccess: false.
- CGPreflightListenEventAccess: false.

A Core Graphics event targeted only at the probe's own PID also delivered no
callbacks/text. Display access and native focus were available; Python's event
posting permission was the blocker. No permissions were changed or requested.
The desktop automation driver had its own functioning native input path. A
throwaway app bundle made the otherwise unbundled Python window addressable by
that driver. A character appeared through native key/character callbacks before
product scenarios began. All test windows were closed afterward.

## OS-delivered scenarios

Offsets below are ASCII document positions unless noted. `|` denotes the caret;
`[text]` denotes a selection. Buffers use LF notation for logical row boundaries.
Every editing/navigation row below had zero input dispatch calls unless specified.

| Initial state and genuine OS action | Observed result |
| --- | --- |
| Empty editable field; type `north`, Return | `north\n`, caret 6; exactly one input `north`, one TEXT_ENTER. No extra blank row. |
| `north\nloo|k dragon\ntail draft`; Shift+Return | Text and caret 9 unchanged; exactly one `look dragon`; zero TEXT_ENTER. |
| `north\n[look dragon]\ntail draft`; Shift+Return | Selection 6–17 preserved; exactly one `look dragon`; zero TEXT_ENTER. |
| `north\n[look\nscore]\ntail draft`; Shift+Return | Text/selection 6–16 preserved; exactly `look`, `score`, once each. |
| `north\ndraft|`; Down four times | Exactly `north\ndraft\n`, caret 12. Existing empty tail reused. |
| Three identical `alpha` rows, caret 17; Up, Down | Caret 11, then 17, with unchanged text. |
| Same rows, caret 17; Shift+Up, Shift+Down | Selection 11–17, then collapsed at 17. No extra tail. |
| Caret at the second row's start; Left/Right and Shift+Left/Right | Native crossing/selection of the separator, with expected 5–6 range and retraction. |
| `north\nl[ook drago]n\ndraft`; Escape | `north\n\nlook dragon\ndraft`, caret 6. Selected characters retained. |
| `ab[cd]ef`; Escape | `ab\ncdef`, caret 1. Original characters retained. |
| `north\n|south`; Backspace | Text and caret 6 unchanged. |
| Same; Shift+Left, Backspace | Selected separator removed: `northsouth`, caret 5. |
| `north|\nsouth`; Shift+Right, Delete | Selected separator removed: `northsouth`, caret 5. Cross-row Shift+Down/Delete also worked. |
| Historical selection, Output focused; Tab | Focus returned to Command at the existing empty tail (10), or the final draft's end (21). Text unchanged. |
| Historical selection, Output focused; type `x` | `x` appended to the final draft at the tail; old selection did not replace text. |
| `look dragon\nlook goblin\nlo|`; Command+R twice | After the local fix: `look goblin`, then `look dragon`; no execution. |
| Historical caret/selection; Command+Return, in Command and in Output | Focused existing tail, no input or autoretype-key dispatch. |
| Standard: type `look`, Return; Up, Down | One `look`, empty compose box; history recalled `look`, then returned to empty. |
| Standard: `look|`, Shift+Return, type `score`, Return | Shift added a newline without sending; Return dispatched `look`, `score` once each and cleared the box. |
| Standard unsent draft; Up, Down, Command+Return | Draft retained; one existing `ctrl+enter` key message, no input dispatch. Engine autoretype behavior remains covered by the separate engine regression. |
| Standard: old selection 1–3 in `abcd draft`, Output focused; type `x` | `axd draft`, caret 2: existing Standard edit-range forwarding preserved. |
| Type `q`; Command+Z, Command+Shift+Z | Undo removed just `q`; redo restored it. |
| Submit `old\nlook|`; undo, redo | Submission buffer change undone/redone; total input calls stayed exactly one. Undo/redo did not execute commands. |
| Output focused; mouse click in first Command row | Focus handler ran, then native click placed caret at 2 rather than the tail. |
| Native Unicode clipboard paste of `я🐉é漢`; Shift+Return | Full text preserved, including non-BMP and combining characters; submitted once unchanged. Native UTF-16 caret was 10 after `old\n` plus this text. |
| Command+C/V and Command+X/V across a separator | Copy, cut and multiline paste preserved remaining drafts and never executed text. The original system clipboard was restored after the test. |
| Local fix loaded: selected `look`, Shift+Return, Return, Down twice | Exactly two `look` calls total, one TEXT_ENTER, final `old\nlook\n`; no repeated empty tails. |

## Discovered bug and fix

Repeated prefix completion failed with real modifier transitions. Cocoa delivered
KEY_DOWN/key-code 0 when Command was pressed **and released**. The optional mode's
"any other key ends recall" logic reset the cycle after the first Command+R.
The next press searched for the entire completed line, producing "No earlier
command matches look goblin" instead of cycling to the older match.

The local fix passes modifier-only events through without resetting completion,
including named modifier keys on other wx ports. It is scoped to editable input;
Standard's handler behavior is unchanged. An added regression reproducing these
modifier transitions failed before the fix and passed afterward. The restarted
native app also passed two separately pressed/released Command+R shortcuts.

Cocoa's wx ControlDown convention maps these application shortcuts to **Command**.
Physical Control+R did not complete, and physical Control+Return opened the native
context menu before a Return event reached wx. Command+R/Return worked. Local
README/help/input-mode documentation now states the macOS keys explicitly; native
Control behavior has not been overridden.

An early undo attempt was invalid because programmatic fixture changes shared the
native undo history. Tests were repeated after restarting the app and clearing
undo history during fixture setup. This was a harness problem, not an observed
product undo failure.

## Limits and repeatability

The desktop driver's Unicode typing attempt delivered only ASCII `e`; the event
trace contained no Cyrillic, emoji, combining-mark or CJK character events. The
failure point within the driver/Cocoa input path was not established. This cannot
establish an optional-mode Unicode keyboard bug or a pass. Unicode clipboard
input and subsequent execution passed. Physical keyboard layouts, Unicode typing,
marked text and IME composition still require a suitable input method/driver.

The regular native wx regression remains synthetic for key delivery. It covers
plugins, word completion, masked passwords, aliases/stacking/autoretype, offline
handling, explicit pack bindings, Unicode position conversion, session/mode
isolation and large buffers. Those interactions were **not** all repeated through
OS input during this review. No Windows, GTK, NVDA, JAWS, Orca or VoiceOver speech
validation occurred.

A new opt-in test uses wx.UIActionSimulator, genuine key release observations,
bounded focus/input deadlines and event-stream quiescence. It covers the main
keyboard scenarios in an isolated child process with a plain-control canary and
recording fake engine. It defaults to skipped and rejects any environment with
CI or GITHUB_ACTIONS set, even when the opt-in flag is inherited. Headless guard
tests verify that neither wx imports nor GUI child processes start in these cases.
It never makes regular CI depend on foreground GUI access. Run it on an
interactive desktop with permitted event posting:

```sh
GENERICMUD_TEST_OS_INPUT=1 uv run pytest -q -rs tests/test_command_entry_os_input.py
```

Here it skips with the precise macOS permission denial. The successful native
coverage above used the desktop driver, not a successful run of this smoke test.
The smoke test itself still needs execution in a simulator-permitted environment.

The temporary recording harness captured 47 assertion snapshots: 43 passed and
four exposed the cycling bug, the two physical-Control platform differences and
the Unicode-driver limitation. Cycling passed after the fix; the other three are
explicitly classified above rather than counted as successful keyboard tests.

Primary API references: [wx.UIActionSimulator](https://docs.wxpython.org/wx.UIActionSimulator.html),
[Apple event-posting preflight](https://developer.apple.com/documentation/coregraphics/cgpreflightposteventaccess%28%29).

## Regression checks

After the fix and test-guard review: `uv run ruff check .` passed;
`uv run pytest -q` reported **2,491 passed, 132 skipped**; all **30** frontend tests and the JavaScript syntax
check passed. `git diff --check` passed. The extra skip is the new opt-in foreground
test. Seven headless tests cover the opt-in/CI guards. Explicitly enabling the GUI
test on this Mac skipped with the two false permission checks shown above;
explicit opt-in with GITHUB_ACTIONS set also skipped before importing wx. These results do not count a skipped simulator test as a pass.

## Remaining Windows/NVDA checks

- Real Ctrl+R cycling and Ctrl+Enter; Enter exactly once; selected Shift+Enter
  without a second send; NVDA focus mode and shortcut interception.
- Windows CRLF/UTF-16 caret mapping, arrows and spoken row/selection feedback,
  boundary Backspace, selected Delete/Backspace, first-row Escape and mouse focus.
- Output/Command Tab and typing; one correctly timed announcement, including
  completion and no-match feedback; no duplicate screen-reader/self-voice speech.
- Password field label, focus, character masking, secret/history/undo isolation
  and restoration of the suspended document/selection.
- Native clipboard and undo/redo, actual keyboard layouts, IME confirmation and
  cancellation, especially Enter during composition; Standard regressions with NVDA.

PR #74 remains draft pending the Windows/screen-reader and IME checks above.
