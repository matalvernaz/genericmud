# genericMud

An accessible, cross-platform, self-voicing MUD client — a modern replacement for
VIPMud. Built for screen-reader users (NVDA > VoiceOver > Orca), with self-voicing
through the user's own synth, modern protocols (GMCP/MSDP/MXP) VIPMud lacks, and a
clean native soundpack format.

Status: **v0.1 in progress.** Design and rationale live in
`~/.claude/plans/can-you-please-help-declarative-badger.md`.

## Architecture

Native Python `asyncio` engine (transport, protocols, automation, voice) + a web UI
(accessible HTML/ARIA chrome + Web Audio soundpacks) over a localhost WebSocket. The
engine core is pure stdlib; native voice and the webview shell are optional extras so
the engine and its tests run anywhere, including headless CI.

```
TCP/TLS → MCCP(zlib) → telnet/IAC → GMCP/MSDP/MSSP/MXP/MSP → ANSI → Line/Buffer
        → triggers/aliases/macros/timers/gags → voice router (self-voice) + soundpacks
        → WebSocket → renderer (output role=log, input, status, dialogs)
```

## Develop

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The engine has no runtime dependencies. Install `.[gui]` for the webview/WebSocket
shell and `.[voice]` for native self-voicing backends (Windows/macOS).
