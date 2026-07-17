# genericMud

An accessible, cross-platform, self-voicing MUD client — a modern replacement for
VIPMud. Built for screen-reader users (NVDA > VoiceOver > Orca), with self-voicing
through the user's own synth, modern protocols (GMCP/MSDP/MXP) VIPMud lacks, and a
clean native soundpack format.

Status: see `genericmud/__init__.py` for the current version. The native wx UI,
pygame audio, the three soundpack dialects (native Lua, VIPMud `.set`,
MUSHclient), the no-code soundpack builder, and world sharing are all live.

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

The engine's runtime dependencies are `lupa` (Lua scripting) and `regex` (ReDoS-safe trigger matching with a
per-match timeout). Install `.[gui]` for the webview/WebSocket shell and `.[voice]` for native self-voicing
backends (Windows/macOS).
