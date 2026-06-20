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

- Speaks via **SAPI5** out of the box (no setup).
- For your **NVDA voice** instead, drop `nvdaControllerClient.dll` (from the NVDA
  Controller Client package, 64-bit) next to the exe / in the project folder.
  The app prefers NVDA when the DLL is present, else SAPI5.

## Known gaps (this is an early test build)

- **Native UI is build-blind** — first run may surface wx/threading issues;
  tell me what NVDA does and I'll fix.
- **Sound effects:** not wired in the native UI yet. The `--web` UI plays MSP
  sounds via `--sounds <dir>`; native audio is the next step.
- **Script packs:** VIPMud `.set` packs load but advanced commands
  (`#if`/`#math`/`#alarm`) don't run yet; flagship MUSHclient packs don't import.

## Keys (native UI)

- **Tab / Shift+Tab** move between the output box and the command box. In the
  output box, read with NVDA as usual (arrows, say-line, say-all); start typing
  and you jump straight to the command box.
- **Enter** sends; **Up/Down** = command history.
- **Ctrl+N** connect (with saved worlds); **Ctrl+W** close the current MUD tab;
  each MUD is its own tab (**Ctrl+PageUp/PageDown** to switch).
- **Ctrl+1..9** recall recent messages; **Alt+arrows** review by line/word/char.
- **F11** / **Esc** stop speech.
