# Running genericMud on Windows

A prebuilt `.exe` can't be produced on the Linux dev host (PyInstaller doesn't
cross-compile, and the native deps are Windows-only). Build/run it on Windows.

## Fastest: test from source (recommended first)

Double-click **`run.bat`**, or from a terminal:

```bat
run.bat                 :: connects to 127.0.0.1:4000
run.bat mud.example.com 4000
run.bat mud.example.com 4000 --tls
run.bat mud.example.com 4000 --sounds C:\path\to\sounds
```

It creates a venv, installs deps, and launches the window. First run takes a
minute (downloads wheels: lupa, pywebview, websockets, pywin32).

Requirements:
- **Python 3.12+** (`py` launcher or `python` on PATH).
- **Microsoft Edge WebView2 Runtime** — preinstalled on current Windows 10/11;
  if missing, get the Evergreen runtime from Microsoft.

## Make a standalone .exe

Double-click **`build_windows.bat`** (or run it). Output: `dist\genericMud.exe`.
Run it from a terminal so you can pass the world: `genericMud.exe host 4000`.

## Voice

- Speaks via **SAPI5** out of the box (no setup).
- For your **NVDA voice** instead, drop `nvdaControllerClient.dll` (from the NVDA
  Controller Client package, 64-bit) next to the exe / in the project folder.
  The app prefers NVDA when the DLL is present, else SAPI5.

## Known gaps (this is an early test build)

- **Sound:** MSP/server-driven sounds now play — pass `--sounds <dir>` at your
  sound files. Full script-based pack import is partial: VIPMud `.set` packs load
  but advanced commands (`#if`/`#math`/`#alarm`) don't run yet; flagship MUSHclient
  packs don't import (they're full MUSHclient apps).
- **Unverified on Windows** — first real run may surface issues with the
  WebView2 window or key passthrough; report what NVDA does and I'll adjust.
- No settings/connect UI yet: pass the world on the command line.

## Keys (VIPMud-familiar)

- Type a command, **Enter** to send. **Up/Down** = command history.
- **Ctrl+1..9** = recall the last nine messages.
- **Alt+Up/Down** = previous/next line; **Alt+Left/Right** = word;
  **Alt+Shift+Left/Right** = character; **Alt+Home/End** = top/bottom.
- **F11** or **Esc** = stop/flush speech.
