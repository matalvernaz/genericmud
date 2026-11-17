"""BASS device discovery and stream-creation context, shared safely by native sessions.

Devices live for the process, like bass_library.start(). A session frees only its streams:
freeing a device on tab close would also free another tab's cues on that device.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from contextlib import contextmanager

from genericmud.config.audio_routes import AudioOutput
from genericmud.sound import bass_library as lib


class AudioRouteUnavailable(RuntimeError):
    """An explicit destination cannot play; it must never fall back to another output."""


_lock = threading.RLock()  # serialize initialization; BASS selection itself is thread-local


def _check_platform() -> None:
    # Endpoint IDs and CoreAudio UIDs identify hardware. ALSA names may instead name a
    # numbered slot or a configurable alias; don't promise a fixed physical destination.
    if sys.platform not in ("win32", "darwin"):
        raise AudioRouteUnavailable("Audio device selection is supported on Windows and macOS")


def _devices(core) -> list[tuple[int, AudioOutput, int]]:
    result = []
    # 0 is the silent device; a pseudo-default without a driver ID isn't a physical endpoint.
    for index in range(1, 1025):
        info = lib.DeviceInfo()
        if not core.BASS_GetDeviceInfo(index, ctypes.byref(info)):
            break
        if info.driver and info.name:
            try:
                identifier = info.driver.decode("utf-8")
            except UnicodeError as error:
                raise AudioRouteUnavailable(
                    "Cannot read an audio output's device identifier"
                ) from error
            result.append((index, AudioOutput(
                sys.platform, identifier,
                info.name.decode("utf-8", errors="replace"),
            ), info.flags))
    return result


def available_outputs() -> list[AudioOutput]:
    """Discover enabled physical endpoints; absence of BASS is an explicit UI error."""
    _check_platform()
    with _lock:
        bass = lib.load()
        lib.start(bass)
        return [output for _, output, flags in _devices(bass.core) if flags & lib.DEVICE_ENABLED]


@contextmanager
def output_context(core, output: AudioOutput | None = None):
    """Select only while creating a stream, then restore this thread's prior selection.

    Resolve the stable identifier on *each* play: hotplug can change enumeration indices.
    An inactive returning device is reinitialized without freeing other sessions' streams.
    """
    with _lock:
        before = core.BASS_GetDevice()
        try:
            if output is None:
                index = lib._default_device
                if index is None or not core.BASS_SetDevice(index):
                    raise AudioRouteUnavailable("System default audio output is unavailable")
            else:
                _check_platform()
                matches = [(index, flags) for index, device, flags in _devices(core)
                           if device.platform == output.platform
                           and device.identifier == output.identifier
                           and flags & lib.DEVICE_ENABLED]
                if len(matches) != 1:
                    raise AudioRouteUnavailable(f"Audio output unavailable: {output.name}")
                index, flags = matches[0]
                selected = bool(core.BASS_SetDevice(index)) if flags & lib.DEVICE_INIT else False
                if not selected or not core.BASS_IsStarted():
                    reinit = lib.DEVICE_REINIT if flags & lib.DEVICE_INIT else 0
                    if not core.BASS_Init(index, 44100, reinit, None, None):
                        raise AudioRouteUnavailable(
                            f"Cannot open audio output {output.name} (BASS error "
                            f"{core.BASS_ErrorGetCode()})"
                        )
                if not core.BASS_SetDevice(index):
                    raise AudioRouteUnavailable(f"Cannot select audio output: {output.name}")
            yield index
        finally:
            # No BASS_Free here: it destroys every stream on that device, across all tabs.
            core.BASS_SetDevice(before)
