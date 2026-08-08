# genericMud on Windows

Most people don't need this file: grab `genericMud-windows.zip` from the
[Releases page](https://github.com/matalvernaz/genericmud/releases), unzip,
run `genericMud.exe`. Worlds, soundpacks, and logs live in `genericmud-data`
beside the exe. How to actually use the client — connecting, keys, soundpacks
— is in `README.md` and in the app under the **Help** menu.

This file is for running from source or building the exe yourself.

## Run from source

Double-click **`run.bat`**, or from a terminal:

```bat
run.bat                              :: open the window (Ctrl+N new, Ctrl+O saved)
run.bat mud.example.com 4000         :: auto-connect a tab
run.bat mud.example.com 4000 --tls
run.bat mud.example.com 4000 --web   :: the alternate web UI
```

It creates the environment, installs dependencies, and launches. The first run
takes a minute while wheels download (wxPython, lupa, prismatoid, pywin32...).

You need **[uv](https://docs.astral.sh/uv/)** — `winget install --id=astral-sh.uv -e`.
uv downloads Python 3.12 itself, so no separate Python install is required, and
it installs the exact versions pinned in `uv.lock`. Nothing else — the WebView2
runtime only matters for `--web`.

## Build a standalone exe

Double-click **`build_windows.bat`**. The result lands in
`dist\genericMud\genericMud.exe`. It is a windowed application, so it opens
the client without a second terminal window. Official zips use the same
PyInstaller layout and GUI subsystem.

## Voice

Output speaks through **your running screen reader** — NVDA, JAWS, ZDSR,
System Access and others, in your own voice and settings, via
[prism](https://github.com/ethindp/prism) — and falls back to the Windows
voice (OneCore/SAPI5) when none is running.
**Ctrl+M** turns self-voice off if you'd rather read the output box with
NVDA directly (Tab to it, then arrow / say-line as usual).

## Known gaps

- The wx UI is written blind and can't be exercised on the Linux dev host —
  if NVDA does something odd, say what you heard and it gets fixed.
- VIPMud `.set` packs run (`#if`, `#alarm`, gags, sounds); `#math`, `#wait`,
  and the `%function()` library don't yet. MUSHclient packs load behind a
  per-pack trust prompt.
