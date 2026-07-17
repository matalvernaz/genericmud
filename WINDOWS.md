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
run.bat                              :: open the window (Ctrl+N to connect)
run.bat mud.example.com 4000         :: auto-connect a tab
run.bat mud.example.com 4000 --tls
run.bat mud.example.com 4000 --web   :: the alternate web UI
```

It creates a venv, installs dependencies, and launches. The first run takes a
minute while wheels download (wxPython, lupa, pywin32...).

You need **Python 3.12 or newer** (the `py` launcher or `python` on PATH).
Nothing else — the WebView2 runtime only matters for `--web`.

## Build a standalone exe

Double-click **`build_windows.bat`**. The result lands in
`dist\genericMud\genericMud.exe`. (Official zips are built the same way by
the GitHub Actions workflow on every release tag.)

## Voice

Output speaks through **your running screen reader** — NVDA or JAWS, in your
own voice and settings — and falls back to SAPI5 when neither is running.
**Ctrl+M** turns self-voice off if you'd rather read the output box with
NVDA directly (Tab to it, then arrow / say-line as usual).

## Known gaps

- The wx UI is written blind and can't be exercised on the Linux dev host —
  if NVDA does something odd, say what you heard and it gets fixed.
- VIPMud `.set` packs run (`#if`, `#alarm`, gags, sounds); `#math`, `#wait`,
  and the `%function()` library don't yet. MUSHclient packs load behind a
  per-pack trust prompt.
