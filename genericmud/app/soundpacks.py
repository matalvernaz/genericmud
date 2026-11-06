from __future__ import annotations

import traceback
from collections.abc import Callable

from genericmud.bridge import protocol
from genericmud.packs import ActivationResult, activate_world
from genericmud.packs.pack_updates import PackUpdates
from genericmud.packs.setup import GitUpdateResult
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.voice.router import SYSTEM_CHANNEL

_PLUGIN_TICK_SECONDS = 0.25  # OnPluginTick cadence (MUSHclient ticks faster; see _arm_plugin_ticks)


_MAX_SIMULATE_DEPTH = 8  # Simulate within a simulated line, this many deep at most
_STATE_SAVE_DELAY_SECONDS = 2.0  # a pack's SaveState is written this soon after it asks


class SoundpacksMixin:
    def pack_notepads(self) -> dict[str, str]:
        """The MUSHclient notepads this session's packs have written, by title (a copy).
        Mush-Z keeps its channel log in one and never opens it itself. Loop thread."""
        notepads: dict[str, str] = {}
        for pack in self._mush_packs:
            notepads.update(pack.notepads())
        return notepads

    def _ask_player(
        self, request: dict, while_waiting: Callable[[], None] | None = None
    ) -> object | None:
        """Hand a pack's question to the window; with no window, or if it fails, it's Cancel."""
        asker = self.asker
        if asker is None:
            return None
        try:
            return asker(request, while_waiting)
        except Exception as exc:  # noqa: BLE001 - a failed dialog must not kill the pack's script
            if self._diag is not None:
                self._diag.event(
                    "pack.ask.error", kind=request.get("kind"),
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            return None

    # --- soundpacks ---

    def activate_packs(self, world: str) -> ActivationResult | None:
        """Load the packs enabled for ``world`` and announce the outcome aloud.

        Call after constructing the app and before connecting, so triggers are
        armed when data arrives. No-op (returns None) when no store is wired.
        """
        if self.packs is None:
            return None
        result = activate_world(self.packs, world, self.engine)
        self._last_activation = result
        self._mush_packs = [
            pack for pack in result.packs.values() if isinstance(pack, MushclientPack)
        ]
        self._vip_packs = [pack for pack in result.packs.values() if isinstance(pack, VipMudPack)]
        self._vip_connected = False
        self._mush_connected = False
        self._pack_timers_cancelled = False
        self._announce_activation(result)
        return result

    def on_focus_changed(self, focused: bool) -> None:
        """This world came to the front (the selected tab, while genericMud's window is the
        active one) or went behind. MUSHclient packs hear each change, as MUSHclient tells
        a world's handlers and plugins; packs installed later hear it as they arrive."""
        if focused == self._focused:
            return
        self._focused = focused
        for pack in self._mush_packs:
            pack.dispatch_focus(focused)

    def _check_pack_updates(self, result: ActivationResult | None) -> None:
        """Ask this world's packs' repositories whether they have updates, in the background
        (each at most once a day), so a pack with one can say so."""
        if self.packs is None or result is None:
            return
        self._world_packs = [*result.loaded, *result.skipped_untrusted, *result.failed]
        if self.pack_updates is None:
            self.pack_updates = PackUpdates(
                self.packs,
                announce=lambda text: self._speak_system(text, interrupt=False),
                installed=self._packs_updated,
                diag=self._diag,
            )
        self.pack_updates.check_due(self._world_packs)

    def update_packs(self, *, quiet: bool = False) -> None:
        """Install whatever this world's packs' repositories have new (/update, or a pack's
        own update command)."""
        if self.pack_updates is None:
            if not quiet:
                self._speak_system("There are no soundpacks to update in this session.")
            return
        self.pack_updates.install(self._world_packs, quiet=quiet)

    def check_pack_updates(self) -> None:
        """Say whether this world's packs' repositories have anything new (a pack's own
        "update check")."""
        if self.pack_updates is None:
            self._speak_system("There are no soundpacks to update in this session.")
            return
        self.pack_updates.check_now(self._world_packs)

    def _packs_updated(self, results: dict[str, GitUpdateResult], quiet: bool) -> None:
        """Tell each updated MUSHclient pack, the way its own updater plugin told it, so it
        plays its update sounds and shows its changelog."""
        activation = self._last_activation
        if activation is None:
            return
        for pack_id, result in results.items():
            pack = activation.packs.get(pack_id)
            if not isinstance(pack, MushclientPack):
                continue
            try:
                pack.pack_updated(result, quiet=quiet)
            except Exception as exc:  # noqa: BLE001 - a pack's handler must not stop the rest
                if self._diag is not None:
                    self._diag.event("pack.update_notice_failed", id=pack_id, error=repr(exc))

    def _vip_connection_changed(self, connected: bool) -> None:
        """Run the VIPMud packs' OnConnect or OnDisconnect alias, once per change, and the
        MUSHclient packs' connect and disconnect hooks.

        The unofficial Prometheus pack loads its settings and switches its sound classes on
        in OnConnect and saves them in OnDisconnect, so without these it stays silent and
        forgets every setting.
        """
        self._mush_connection_changed(connected)
        if connected == self._vip_connected:
            return
        self._vip_connected = connected
        for pack in self._vip_packs:
            try:
                pack.connection_changed(connected)
            except Exception as exc:  # noqa: BLE001 - one pack's hook must not stop the rest
                if self._diag is not None:
                    self._diag.event(
                        "vipmud.connection_hook.error", connected=connected,
                        error=f"{type(exc).__name__}: {exc}",
                        traceback=traceback.format_exc(),
                    )

    def _mush_connection_changed(self, connected: bool) -> None:
        """Run the MUSHclient packs' OnPluginDisconnect or OnPluginConnect, once per change.

        Their first connect is announced along with their install hook when the session
        starts; after that they hear every drop and reconnect, as in MUSHclient.
        """
        if connected == self._mush_connected:
            return
        self._mush_connected = connected
        resume = connected and self._pack_timers_cancelled
        if resume:
            self._pack_timers_cancelled = False
        for pack in self._mush_packs:
            if connected:
                if resume:
                    pack.resume_timers()  # only after a drop that cancelled them
                pack.dispatch_connect()
            else:
                pack.dispatch_disconnect()

    def _simulate(self, text: str) -> None:
        """A pack's Simulate: the text goes through triggers, the output and speech as if
        the MUD had sent it, a line at a time. A trigger simulating the line it matched would
        go round forever, so it stops a few deep."""
        if self._simulate_depth >= _MAX_SIMULATE_DEPTH:
            if self._diag is not None:
                self._diag.event("pack.simulate", status="refused", reason="nested too deep")
            return
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and not lines[-1]:
            lines.pop()  # the text's own line ending
        self._simulate_depth += 1
        try:
            for line in lines:
                self._emit_line(line)
        finally:
            self._simulate_depth -= 1

    def _request_save(self) -> None:
        """A pack's SaveState: save the packs' variables shortly. Aardwolf's pack saves on
        every setting it changes, so asks close together make one write."""
        if self._save_scheduled or self._closed:
            return
        self._save_scheduled = True

        def save() -> None:
            self._save_scheduled = False
            if not self._closed:
                self._persist_pack_vars()

        self._schedule(_STATE_SAVE_DELAY_SECONDS, save)

    def _dispatch_plugin_install(self) -> None:
        """Run each MUSHclient pack's install hook (MUSHclient calls it as the world
        opens; packs set their variable defaults in OnPluginInstall -- Erion turns every
        sound toggle on there, so skipping it leaves the pack gated silent). Their connect
        hook follows once the connection is open (on_socket_open)."""
        if not self._mush_packs:
            return
        if self._diag is not None:
            # NB: event()'s first positional is named `stage` -- a stage= kwarg here
            # collides and raises (the 0.6.6 silent-session bug).
            self._diag.event("plugin.lifecycle", phase="install",
                             packs=len(self._mush_packs))
        for pack in self._mush_packs:
            pack.dispatch_install()
        if self._focused:  # the world is in front as its packs arrive, as in MUSHclient
            for pack in self._mush_packs:
                pack.dispatch_focus(True)
        if self._msdp_offered:  # negotiation happened before packs were ready: replay it
            self._dispatch_msdp_start()
        self._arm_plugin_ticks()
        if self._diag is not None:
            self._diag.event("plugin.lifecycle.done")

    def _arm_plugin_ticks(self) -> None:
        """Run each pack's ``OnPluginTick`` on a repeating schedule.

        MUSHclient ticks continuously; Erion's tick IS its music/ambience engine --
        StartMusic/StartWeather/StartAmbiance restart a finished (non-looping) ambience
        and rotate weather. Without it, ambience plays once per room and dies. 0.25s is
        far below MUSHclient's rate but ambience-restart latency is imperceptible, and
        each dispatch is a few variable reads. Skipped while disconnected so a tick
        can't restart music after quit (the flush would be fighting the tick).
        """
        if self._ticks_armed or not any(p.has_hook("OnPluginTick") for p in self._mush_packs):
            return
        self._ticks_armed = True

        def tick() -> None:
            if self._closed:
                return  # session torn down: let the chain end
            if self.engine.connected:
                for pack in self._mush_packs:
                    pack.dispatch("OnPluginTick")
            self._schedule(_PLUGIN_TICK_SECONDS, tick)

        self._schedule(_PLUGIN_TICK_SECONDS, tick)

    def _announce_activation(self, result: ActivationResult) -> None:
        triggers = sum(
            len(bucket["trigger"]) for bucket in self.engine.registrations_by_source().values()
        )
        if self._diag is not None:
            self._diag.event(
                "pack.summary",
                loaded=",".join(result.loaded) or "none",
                failed=",".join(result.failed) or "none",
                untrusted=",".join(result.skipped_untrusted) or "none",
                conflicts=len(result.conflicts),
                triggers=triggers,
                sppath=self.engine.get_var("sppath") or "",
            )
        parts: list[str] = []
        if result.loaded:
            count = len(result.loaded)
            parts.append(f"{count} soundpack{'s' if count != 1 else ''} loaded")
        # Packs loaded but armed no triggers can never make a sound -- say so at connect, not
        # only in the trace, so a blind user learns the pack is inert without reading a file.
        if result.loaded and triggers == 0:
            parts.append("packs loaded but no triggers registered")
        for pack_id in result.skipped_untrusted:
            parts.append(
                f"{pack_id} not loaded because it isn't trusted yet — trust it in Manage "
                "Soundpacks, then reconnect"
            )
        for pack_id, error in result.failed.items():
            parts.append(f"{pack_id} failed to load: {error}")
        for conflict in result.conflicts:
            who = " and ".join(conflict.sources)
            parts.append(f"{conflict.kind} {conflict.token} bound by {who}")
        if not parts:
            return
        summary = "; ".join(parts)
        self.voice.speak(summary, channel=SYSTEM_CHANNEL, interrupt=False)
        self._post(protocol.echo(f"* {summary}"))
