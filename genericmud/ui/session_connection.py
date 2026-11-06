"""A tab's engine and connection, run on the loop thread.

Starting the engine, sending, disconnecting and reconnecting, moving to an encrypted port the MUD
offers, and closing the tab.
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import replace

import wx

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.worlds import load_worlds
from genericmud.sound.bass_backend import make_sound_backend
from genericmud.transport.connection import MudConnection
from genericmud.voice.factory import make_voice_backend
from genericmud.voice.router import VoiceRouter


class SessionConnectionMixin:
    """A tab's engine and connection, for SessionPanel (session_panel.py).

    Starts the engine on the loop thread and wires its callbacks, then disconnects, reconnects,
    moves to an encrypted port and closes the tab. Uses these attributes from
    SessionPanel.__init__: _alive, _connect_finished, _connection, _credentials, _diag, _focused,
    _hub, _keymap, _loop, _on_pref, _packs, _prefs, _questions, _reconnecting, _sound_warned,
    _voice, app and world. Calls into output_box (_handle_message) and pack_window (notepads and
    pack questions).
    """

    async def _start(self) -> None:
        # run_coroutine_threadsafe's future is discarded, so an exception below would
        # otherwise vanish entirely: the loop exception handler never fires (the future
        # counts as having retrieved it), the crash log stays banner-only, and the tab
        # just sits dead -- the one failure shape a blind user can't detect. Trace it
        # and say it aloud instead.
        try:
            await self._start_inner()
        except Exception as error:  # noqa: BLE001 - surface session-setup death, never swallow
            if not self._alive:
                return
            if self._diag is not None:
                self._diag.event(
                    "session.start_failed",
                    error=f"{type(error).__name__}: {error}",
                    traceback=traceback.format_exc(),
                )
            self._post(protocol.echo(f"* Session failed to start: {error}"))
            if self._voice is not None:
                self._voice.speak(
                    f"Session failed to start: {type(error).__name__}",
                    channel="system",
                    interrupt=True,
                )

    async def _start_inner(self) -> None:
        if not self._alive:
            return  # teardown ran before startup got its turn on the loop
        self._voice = VoiceRouter(make_voice_backend())
        self._connection = MudConnection()
        self.app = EngineApp(
            self._voice,
            send=self._send,
            send_raw=self._send_raw,
            post=self._post,
            schedule=self._loop.call_later,
            keymap=self._keymap,
            packs=self._packs,
            sound_backend=make_sound_backend(on_error=self._sound_error, diag=self._diag),
            name=self.world.name,  # used for the session log filename
            credentials=self._credentials,
            hub=self._hub,
            diag=self._diag,
            suppress_reconnect=self._connection.suppress_reconnect,
            withdraw_option=self._connection.withdraw_remote,
            encoding=self.world.encoding,
            connect_method=self.world.connect_method,
            post_connect_commands=self.world.post_connect_commands,
            mxp=self.world.mxp,
            pueblo=self.world.pueblo,
            other_worlds=lambda: [world.name for world in load_worlds()],
        )
        self.app.on_focus_changed(self._focused)  # what the window said before this existed
        self.app.asker = self._ask_from_pack
        self.app.pack_window = self  # packs' clipboard, command box, notepads and links
        # The address this session reaches may serve its media even on localhost or the LAN.
        self.app.media.trusted_address = self._connection.peer_address
        # One codec for both directions: commands go out in the encoding output is read in.
        self._connection.text_codec = self.app.codec
        # Seed the persisted speech toggles; changes flow back out through pref_sink.
        self.app.follow_mode = self._prefs.follow_mode
        self.app.interrupt_mode = self._prefs.interrupt_mode
        self.app.autoretype = self._prefs.autoretype
        if self._on_pref is not None:
            self.app.pref_sink = self._on_pref
        self._connection._on_event = self.app.on_telnet_event
        self._connection.configure_reconnect(
            self._prefs.auto_reconnect, self._prefs.reconnect_interval,
        )
        # A reconnect the transport starts by itself asks where to dial, so an edited world
        # applies to a drop nobody asked for as well as to Ctrl+D.
        self._connection.set_target_provider(self._target_for_reconnect)
        self._connection.on_status = self.app.on_connection_status
        self.app.apply_world_settings(self.world)  # command stacking and logging
        if self.world.sounds:  # point @sppath at the world's sound folder before packs load
            self.app.engine.set_var("sppath", self.world.sounds)
            self.app.engine.sounds_dir = self.world.sounds  # and trust it, as packs can't be
        # Activate packs and arm auto-login before any data; they hear the MUD connected
        # once the connection opens, below.
        self.app.on_connect(self.world.name, socket_open=False)
        if not self._alive or self._connect_finished:
            return  # Disconnect was requested before startup; keep the tab ready for Reconnect
        if self._diag is not None:
            self._diag.event("connect.begin", host=self.world.host, port=self.world.port)
        try:
            await self._connection.connect(self.world.host, self.world.port, tls=self.world.tls)
            if not self._alive or not self._connection.connected:
                await self._connection.close()
                return  # a closed tab or a cancelled handshake is not a successful connect
            if self._diag is not None:
                self._diag.event("connect.ok", host=self.world.host)
            # First, before anything else can queue up: the socket is finally open, so a
            # blind login (MUSH/MOO, Diku) and the after-connect commands can be typed.
            self.app.after_connect()
            self._post(protocol.echo(f"* Connected to {self.world.name}"))
            # Speak it too: connect failure and reconnect both speak, so success staying
            # echo-only was inconsistent and left a quiet MUD's first moment ambiguous.
            self.app.voice.speak(f"Connected to {self.world.name}.", channel="system")
            # Nothing from the MUD has been read yet: the reading starts once this yields.
            self.app.on_socket_open()
        except OSError as error:
            if not self._alive or self._connect_finished:
                return  # a stopped startup has nobody to report a late failure to
            if self._diag is not None:
                self._diag.event("connect.failed", error=str(error))
            self._post(protocol.echo(f"* Connect failed: {error}"))
            # The engine defaults to connected=True and packs/OnPluginTick read it, so a failed
            # connect must flip it off or ticks run against a socket that never opened. And speak
            # it -- a visual-only echo leaves a blind user with no idea the connect ever failed.
            if self.app is not None:
                self.app.engine.connected = False
            if self._connection.start_reconnect(detail=str(error)):
                return  # transport owns the retries and their single spoken announcement
            if self._voice is not None:
                self._voice.speak(f"Connect failed: {error}", channel="system", interrupt=True)
        finally:
            # Either way the first attempt is over, so the command box can now tell a real
            # disconnect from a handshake still in flight.
            self._connect_finished = True

    def _send(self, text: str) -> None:
        try:
            if self._connection is not None:
                self._connection.send_line(text)
        except ConnectionError:
            pass

    def _send_raw(self, data: bytes) -> None:
        try:
            if self._connection is not None:
                self._connection.send_packet(data)
        except ConnectionError:
            pass

    def _post(self, message: dict) -> None:
        if self._alive:
            wx.CallAfter(self._handle_message, message)

    def _sound_error(self, message: str) -> None:  # loop thread (pygame backend / make_pygame)
        """Surface a sound failure: echo every one to the output, speak only the first.

        A blind user otherwise gets silence with no clue why; the first failure is spoken
        so they know to look, and all of them land in the reviewable output.
        """
        self._post(protocol.echo(f"* {message}"))
        if not self._sound_warned:
            self._sound_warned = True
            if self._voice is not None:
                self._voice.speak(message, channel="system", interrupt=False)

    def use_secure_port(self, port: int) -> bool:  # loop thread
        """Move this world to the encrypted port the MUD advertised, save it, and reconnect.

        The engine asks and the window acts, because the world file and the connection live
        here. Saving the change is the point rather than a side effect: the next session is
        encrypted too, without anyone having to remember a port number.

        The engine asks from its own thread, where no window may be touched (saving can put
        up a message box), so the work is handed to the window's thread.
        """
        if not self._alive or port <= 0 or (self.world.tls and self.world.port == port):
            return False
        wx.CallAfter(self._take_secure_port, port)
        return True

    def _take_secure_port(self, port: int) -> None:  # UI thread
        if not self._alive:
            return
        self.world = replace(self.world, port=port, tls=True)
        # Saved again only if it is in the saved list: a world the player chose not to keep
        # stays unkept, and this tab still switches.
        name = self.world.name.casefold()
        kept = any(saved.name.casefold() == name for saved in load_worlds())
        frame = wx.GetTopLevelParent(self)
        save = getattr(frame, "_save_world", None)
        if kept and save is not None:
            # `replacing` is its own name: editing the world, not clobbering a different one.
            save(self.world, replacing=self.world.name)
        # The engine's own picture of this link decides whether to offer again: without telling
        # it, the next connect would offer the same port a second time and then /secure would
        # answer "set it by hand" about a world already on it.
        if self.app is not None:
            self._loop.call_soon_threadsafe(self.app.apply_world_settings, self.world)
        # reconnect() is written for a dead link and returns at once while the connection is
        # up, which is exactly what a /secure from a live session is. So the live link goes
        # first, and the new port is taken on the loop thread once it has gone.
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._switch_secure_port()))

    async def _switch_secure_port(self) -> None:  # loop thread
        """Close the live link and open it again on the world's new port.

        The close has to finish before the connect starts: reconnect() refuses to begin while
        the old socket still counts as connected, and two read loops over one tab tear each
        other down.
        """
        if not self._alive or self._connection is None or self.app is None:
            return
        self._do_disconnect()
        for _ in range(200):  # five seconds, far longer than closing a local socket takes
            if not self._connection.connected and not self._connection.connecting:
                break
            await asyncio.sleep(0.025)
        self._reconnecting = False
        await self._reconnect()
        if not self._alive or not self._connection.connected:
            return  # the reconnect said what went wrong itself
        # The switch was said before the link dropped, and the disconnect and reconnect lines
        # that follow cut it off mid-word. Queued after them, this is what is left to hear.
        text = f"{self.world.name} is on port {self.world.port} now, encrypted."
        self._post(protocol.echo(f"* {text}"))
        if self._voice is not None:
            self._voice.speak(text, channel="system", interrupt=False)

    def close(self) -> None:
        """Tear down the session; safe to call from the wx thread on tab close."""
        if not self._alive:
            return
        self._alive = False
        self.close_notepads()
        try:
            for question in list(self._questions):  # free the loop thread to tear down
                self._withdraw_question(question)
        except Exception as error:  # noqa: BLE001 - the session must be torn down regardless
            if self._diag is not None:
                self._diag.event(
                    "pack.ask.withdraw_failed", error=f"{type(error).__name__}: {error}"
                )
        self._loop.call_soon_threadsafe(self._teardown)

    def _teardown(self) -> None:  # loop thread
        if self.app is not None:
            self.app.shutdown()  # leave the session hub, stop logging
            self.app.sound.flush()  # a looping ambience/music cue outlives the tab otherwise
        if self._voice is not None:
            self._voice.flush()
        if self._connection is not None:
            self._connection.request_close()

    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.connected

    def is_connecting(self) -> bool:
        return not self._connect_finished or self._reconnecting or (
            self._connection is not None and self._connection.connecting
        )

    def disconnect(self) -> None:
        """Drop the connection but keep the session/tab open (and stop auto-reconnect)."""
        self._loop.call_soon_threadsafe(self._do_disconnect)

    def apply_connection_settings(self, enabled: bool, interval: int) -> None:
        self._loop.call_soon_threadsafe(self._apply_connection_settings, enabled, interval)

    def _apply_connection_settings(self, enabled: bool, interval: int) -> None:
        if self._alive and self._connection is not None:
            self._connection.configure_reconnect(enabled, interval)

    def _do_disconnect(self) -> None:  # loop thread
        self._connect_finished = True  # also stop an initial connect that hasn't started yet
        if self._connection is not None:
            self._connection.request_close()
        if self.app is not None:
            # request_close() marks the intent now, so even a ready EOF stays quiet on the
            # transport side. The flush, timer cancel, hidden-input reset, VIPMud packs'
            # OnDisconnect and wording still come from the same path a dropped link uses.
            # Reporting it through that path keeps the two identical instead of
            # maintaining a second copy of them here.
            self.app.on_connection_status("disconnected")

    def reconnect(self) -> None:
        """Reconnect this session in place, keeping its tab, scrollback and loaded packs.

        It is the way back after a deliberate disconnect (which stops the retries on purpose),
        with automatic reconnection switched off in Connection settings, and after a protocol
        error. Opening the world again from Ctrl+O is not a substitute -- it builds a second
        tab and abandons this one.
        """
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._reconnect()))

    def _target_for_reconnect(self) -> tuple[str, int, bool]:  # loop thread
        """Where an automatic reconnect should go: the world as it stands now."""
        self._adopt_saved_world()
        return (self.world.host, self.world.port, self.world.tls)

    def _adopt_saved_world(self) -> None:  # loop thread
        """Take this world's connection details from the saved world, as they stand now.

        How a tab connects used to be frozen at the moment it opened, so editing a world's port
        or TLS switch and pressing Ctrl+D reconnected to the old ones. The details are read from
        one record at once instead -- host, port, TLS and encoding together -- which is what
        keeps a new host from arriving with the old encoding, the reason the freeze existed.

        Sounds are the exception: packs load when a tab starts, so a new Sounds folder needs a
        new tab rather than a reconnect.
        """
        saved = next(
            (
                world
                for world in load_worlds()
                if world.name.casefold() == self.world.name.casefold()
            ),
            None,
        )
        if saved is None:
            return
        self.world = replace(
            self.world,
            host=saved.host,
            port=saved.port,
            tls=saved.tls,
            encoding=saved.encoding,
        )
        if self.app is not None:
            # On the loop thread, where the engine lives: the codec is replaced in place and the
            # connection re-pointed at the new one, so typed commands go out in it too.
            self.app.set_encoding(self.world.encoding)
            self._connection.text_codec = self.app.codec
            # And the engine's own picture of the link, which an MSSP offer of an encrypted
            # port is checked against: left as it opened, a world edited onto that port and
            # TLS was offered it again, and /secure then said to set by hand what was set.
            self.app.apply_world_settings(self.world)

    async def _reconnect(self) -> None:  # loop thread
        if not self._alive or self._connection is None or self.app is None:
            return  # the tab closed before this got its turn on the loop
        if self._connection.connected:
            return  # the link came back while this waited its turn on the loop
        if self._reconnecting or self._connection.connecting:
            # Already on its way back: this tab's first connect, a reconnect asked for a
            # moment ago, or the transport's own backoff between tries. A second connect
            # would open a second socket, and the two read loops would then tear down each
            # other's sockets and restart the backoff without end. Say so, so the key press
            # isn't met with silence.
            self._post(protocol.echo(f"* Still connecting to {self.world.name}"))
            if self._voice is not None:
                self._voice.speak(f"Still connecting to {self.world.name}.", channel="system",
                                  interrupt=True)
            return
        # Only now, with no link up and none on its way: the encoding is replaced along with the
        # address, and a live session's text must not change codec under it.
        self._adopt_saved_world()
        self._reconnecting = True
        # Manual reconnect clears the session's stop intent, but respects the global choice.
        self._connection.auto_reconnect = self._prefs.auto_reconnect
        if self._diag is not None:
            self._diag.event("reconnect.manual", host=self.world.host, port=self.world.port)
        self._post(protocol.echo(f"* Reconnecting to {self.world.name}"))
        if self._voice is not None:
            self._voice.speak(f"Reconnecting to {self.world.name}.", channel="system",
                              interrupt=True)
        try:
            await self._connection.connect(self.world.host, self.world.port, tls=self.world.tls)
        except OSError as error:
            if not self._alive:
                return  # the tab closed while this waited; nobody is listening for it
            if self._diag is not None:
                self._diag.event("reconnect.failed", error=f"{type(error).__name__}: {error}")
            self._post(protocol.echo(f"* Reconnect failed: {error}"))
            if self._connection.start_reconnect(detail=str(error)):
                return
            if self._voice is not None:
                self._voice.speak(f"Reconnect failed: {error}", channel="system", interrupt=True)
            return
        finally:
            self._reconnecting = False
        if not self._alive or not self._connection.connected:
            # The tab closed during the handshake. Either its teardown has closed the
            # connection already (connect() then drops the late socket and returns quietly)
            # or it is about to. There is nothing to reattach, and saying "Reconnected"
            # would be untrue, which is what the transport's own loop stopped doing too.
            return
        # The socket is live again. Report it the way the transport's own backoff loop
        # does, for the reason _do_disconnect reports through that path: the per-socket
        # reset, the wording and the VIPMud packs' OnConnect (run after the announcement,
        # so it can't cut off a pack's welcome) all come from one place, not a copy here.
        self.app.on_connection_status("reconnected", manual=True)

    def _prompt_reconnect(self) -> None:
        """Ask to reconnect after a command was typed into a dead session.

        The transport drops commands silently while disconnected, so saying nothing left
        a blind user unable to tell a dead session from a command the MUD ignored.
        """
        dialog = wx.MessageDialog(
            self,
            f"Not connected to {self.world.name}.\n\nReconnect now?",
            "Not connected",
            wx.YES_NO | wx.ICON_QUESTION,
        )
        dialog.SetYesNoLabels("&Reconnect", "&Cancel")
        answer = dialog.ShowModal()
        dialog.Destroy()
        if answer == wx.ID_YES:
            self.reconnect()
