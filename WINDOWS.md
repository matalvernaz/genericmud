# Running genericMud on Windows

A prebuilt `.exe` can't be produced on the Linux dev host (PyInstaller doesn't
cross-compile, and the native deps are Windows-only). Build/run it on Windows.

## Fastest: test from source (recommended first)

Double-click **`run.bat`**, or from a terminal:

```bat
run.bat                              :: native UI, no auto-connect (File > Connect / Ctrl+N)
run.bat mud.example.com 4000         :: auto-connect a tab
run.bat mud.example.com 4000 --tls
run.bat mud.example.com 4000 --web   :: old web UI instead of native
```

It creates a venv, installs deps, and launches the window. First run takes a
minute (downloads wheels: wxPython, lupa, pywin32, ...).

Requirements:
- **Python 3.12+** (`py` launcher or `python` on PATH).
- The native UI needs nothing extra. The WebView2 Runtime only matters for `--web`.

## Make a standalone .exe

Double-click **`build_windows.bat`** (or run it). Output: `dist\genericMud.exe`.
Run it from a terminal so you can pass the world: `genericMud.exe host 4000`.

## Voice

- Streaming output is spoken through **your running screen reader** — NVDA speaks it
  in your own voice and settings (via accessible_output2; no DLL to place). It falls
  back to SAPI5 only if no screen reader is running.
- **Ctrl+M** toggles self-voice off, so you can read the output with NVDA directly
  (Tab to the output box, arrow / say-line) instead.

## Known gaps (this is an early test build)

- **Native UI is build-blind** — a run may surface wx/threading issues;
  tell me what NVDA does and I'll fix.
- **Script packs:** VIPMud `.set` packs run (`#if`, `#alarm`, sounds); `#math`,
  `#wait`, and the `%function()` library don't yet. MUSHclient packs load
  behind a per-pack trust prompt.

## Keys (native UI)

- **Tab / Shift+Tab** move between the output box and the command box. In the
  output box, read with NVDA as usual (arrows, say-line, say-all); start typing
  and you jump straight to the command box.
- **Enter** sends; **Up/Down** = command history. **Ctrl+Enter** toggles
  autoretype: Enter on an empty line resends your last command.
- **Ctrl+Space / Ctrl+Shift+Space** complete the word you're typing from words
  seen in recent output, cycling forward/backward.
- **Numpad** walks (8/2/4/6 + diagonals, 5 or 0 look, `.` scan, `-` up, `+`
  down). Turn it off under View if NVDA's desktop layout needs the numpad.
- **Ctrl+N** connect (with saved worlds); **Ctrl+W** close the current MUD tab;
  each MUD is its own tab (**Ctrl+Tab / Ctrl+Shift+Tab** to switch).
- **Ctrl+1..9** recall recent messages; **Alt+arrows** review by line/word/char;
  **Alt+Shift+Enter** spells the current line character by character.
- **Ctrl+Alt+Left/Right** cycle your chat channels; **Ctrl+Alt+Up/Down** scroll
  within one; **Ctrl+Alt+1..9** recall that channel's recent messages.
- **Ctrl+F** follow mode (speech interrupts when you move rooms, not on every
  line); **Ctrl+I** interrupt mode (every line barges in instead of queueing).
- **F11** / **Esc** stop speech; **Shift+F11** stops all sounds.
- **View menu:** Background silence (stay quiet while you're in another window —
  triggers and sounds keep running) and Numpad compass. Both stick across runs.

## Sharing a world

**File > Export This World...** saves the current world — connection details,
builder triggers/aliases/hotkeys/channels, and every copied sound — as one zip.
A friend uses **File > Import a World...** and it appears in their Connect
dialog, sounds and all.
