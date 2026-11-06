"""GMCP, MSDP and MSSP data, soundpack notifications and encrypted-port offers."""

from __future__ import annotations

import time

from genericmud.automation.variables import VariableEntry, list_variables
from genericmud.bridge import protocol
from genericmud.protocol import msdp
from genericmud.protocol import telnet as T
from genericmud.protocol.oob import OobMessage, ServerStatus, from_subnegotiation
from genericmud.voice.router import SYSTEM_CHANNEL

# MSDP variables that mean the player's location changed; both the table and flat forms.
_MSDP_ROOM_VARS = frozenset(
    {"ROOM", "ROOM_NAME", "ROOM_AREA", "ROOM_EXITS", "ROOM_VNUM", "AREA_NAME"}
)


# Ports a game may send a player to over MSSP: the ordinary range, plus HTTPS, which games do
# use for TLS. Anything else privileged is refused -- taking a status reply's word for it would
# write the number into the world file and dial it.
_ALLOWED_LOW_PORTS = frozenset({443})


class MudDataMixin:
    def variable_listing(self) -> tuple[list[VariableEntry], bool]:
        """Every value this session knows, for the MUD Variables dialog and the picker.

        Reads live engine state, so call it on the loop thread; the rows are plain frozen
        values, safe to hand to the UI thread afterwards.
        """
        return list_variables(
            self.engine.all_mud_vars(), self.engine.all_vars(),
            arrivals=self.engine.data_arrival_times(), now=time.monotonic(),
        )

    def _dispatch_msdp_start(self) -> None:
        """Open the MSDP tap (the transport already answered DO).

        Two subscribers, and both have to ask or the server stays silent. An MSDP soundpack
        sends its own REPORT list on the SENT_DO round -- Erion's server streams nothing
        until those arrive. The client itself asks too, or a MUD that speaks MSDP instead of
        GMCP leaves every ${mud:...} reference, gauge and room lookup empty with no error.
        """
        if self._diag is not None:
            self._diag.event("msdp.start", packs=len(self._mush_packs))
        self._send_msdp(msdp.CMD_LIST, msdp.REPORTABLE_VARIABLES)
        for pack in self._mush_packs:
            pack.dispatch_telnet_request(T.OPT_MSDP, "WILL")
            pack.dispatch_telnet_request(T.OPT_MSDP, "SENT_DO")

    def _send_msdp(self, command: str, value: str) -> None:
        self.sink.send_packet(T.subnegotiation(T.OPT_MSDP, msdp.encode_msdp(command, value)))

    def _subscribe_msdp(self, reportable: object) -> None:
        """REPORT the variables we can use that this server actually advertises.

        Asking for a variable a server doesn't have gets an error line on some codebases and
        is ignored on others, so intersect rather than guess. Ordered by our list, not the
        server's, so the request is stable across sessions.
        """
        if not isinstance(reportable, list):
            return  # a scalar or table here is a non-conforming server; nothing safe to ask for
        available = {str(name) for name in reportable}
        wanted = [name for name in msdp.STANDARD_REPORTS if name in available]
        if self._diag is not None:
            self._diag.event("msdp.subscribe", offered=len(available), wanted=len(wanted))
        for name in wanted:
            self._send_msdp(msdp.CMD_REPORT, name)

    def _handle_subnegotiation(self, sub: T.Subnegotiation) -> None:
        result = from_subnegotiation(
            sub.option, sub.payload, self.codec.decode_payload, self.codec.decode_gmcp
        )
        if isinstance(result, list):
            msdp_room_changed = False
            for message in result:
                if isinstance(message, OobMessage):
                    self._gauges[message.name] = message.value
                    self.engine.set_mud_var(message.name, message.value)
                    self.engine.set_mud_var(f"{message.source}.{message.name}", message.value)
                    # Once per message, with both names stored, so a rule that fires on it
                    # can already read the new value through ${mud:...}.
                    self.engine.data_arrived(message.source, message.name, message.value)
                    if message.source == "gmcp":
                        for pack in self._mush_packs:
                            pack.dispatch_gmcp(message.name, message.value)
                        if message.name.lower() == "core.goodbye":
                            self._server_goodbye(message.value)
                        elif message.name.lower() == "room.wrongdir":
                            self._wrong_direction(message.value)
                        elif message.name.lower().split(".")[:2] == ["client", "media"]:
                            self._client_media(message.name, message.value)
                    elif message.name == msdp.REPORTABLE_VARIABLES:
                        self._subscribe_msdp(message.value)
                    elif message.name in _MSDP_ROOM_VARS:
                        msdp_room_changed = True
                    if self._is_room_info(message):
                        self._update_room(message.value)
            # Once per packet, not once per variable: a server reports every changed variable
            # together, so updating per message would announce one move several times over.
            if msdp_room_changed and (room := self._msdp_room()):
                self._update_room(room)
            if result:
                self._post(protocol.status(self._gauges))
        elif isinstance(result, ServerStatus):
            self._gauges.update(result.data)
            for name, value in result.data.items():
                self.engine.set_mud_var(name, value)
                self.engine.set_mud_var(f"mssp.{name}", value)
                self.engine.data_arrived("mssp", name, value)
            self._note_secure_port(result.data)
            self._post(protocol.status(self._gauges))
        if sub.option == T.OPT_MSDP and self._mush_packs:
            self._route_msdp(sub.payload)

    def _note_secure_port(self, data: dict[str, str]) -> None:
        """Offer the encrypted port the MUD advertises, when this link isn't already on it.

        MSSP is where a game puts the port it never prints in its banner -- both MUDs checked
        by hand here hid it there, one behind an in-game FAQ and one behind nothing at all. It
        is the one thing a player cannot guess, so it is said once, in passing, with the
        command that takes it. Nothing is changed on the strength of it: moving a world to a
        different port is the player's decision.
        """
        if self._secure_port is not None or self._world_tls:
            return
        advertised = ""
        for name, value in data.items():
            if name.strip().upper() in ("SSL", "TLS"):
                advertised = str(value).strip()
                break
        # isdecimal, not isdigit: "²" is a digit that int() refuses, and so is a number longer
        # than Python will convert. Either would stop this MSSP reply half handled.
        port = int(advertised) if advertised.isdecimal() and len(advertised) <= 5 else 0
        plausible = port in _ALLOWED_LOW_PORTS or 1024 <= port <= 65535
        if not plausible or port == self._world_port:
            if advertised and self._diag is not None:
                self._diag.event("mssp.secure_ignored", value=advertised)
            return
        self._secure_port = port
        if self._diag is not None:
            self._diag.event("mssp.secure_offered", port=port, mssp=sorted(data)[:6])
        self._speak_system(
            f"this game offers an encrypted connection on port {port}; /secure switches this "
            "world to it",
            interrupt=False,
        )

    def _use_secure_port(self) -> None:
        """Take up the encrypted port the MUD offered: the window saves the world and reconnects.

        The window owns the world file and the connection, so the engine asks rather than
        acting; where there is no window to ask (the web UI), it says what to change by hand.
        """
        port = self._secure_port
        if port is None:
            self._speak_system("this game has not offered an encrypted port")
            return
        if self._world_tls and self._world_port == port:
            # Already taken: the offer stood before this world was moved onto that port.
            self._speak_system(f"this world is already using its encrypted port, {port}")
            return
        hook = getattr(self.pack_window, "use_secure_port", None)
        if hook is not None and hook(port):
            if self._diag is not None:
                self._diag.event("mssp.secure_taken", port=port)
            self._speak_system(f"switching this world to port {port}, encrypted")
            return
        if self._diag is not None:
            self._diag.event("mssp.secure_unavailable", port=port)
        self._speak_system(
            f"set this world's port to {port} and tick Use TLS in its details"
        )

    def _route_msdp(self, payload: bytes) -> None:
        """Hand a raw MSDP payload to each MUSHclient pack's telnet-subnegotiation
        hook (Erion's MSDP_handler parses it and plays the matching cue)."""
        self._msdp_routed += 1
        if self._diag is not None and (self._msdp_routed <= 50 or self._msdp_routed % 200 == 0):
            # Full trace for the first packets (the diagnostic window), then sampled --
            # MSDP streams continuously and would otherwise fill the log's byte cap.
            self._diag.event(
                "msdp.route", n=self._msdp_routed, size=len(payload),
                preview=repr(payload[:40]), packs=len(self._mush_packs),
            )
        for pack in self._mush_packs:
            pack.dispatch_telnet_subnegotiation(T.OPT_MSDP, payload)

    def _msdp_room(self) -> dict:
        """The navigator's room shape from MSDP's variables, or ``{}`` if none are set.

        Servers report either a ``ROOM`` table or flat ``ROOM_*`` variables, and MSDP names
        are uppercase, so neither form reaches ``Navigator.where()`` untranslated. VNUM is
        carried because two rooms can share a name and it's what makes a move detectable.
        """
        table = self._gauges.get("ROOM")
        if not isinstance(table, dict):
            table = {}
        room = {
            "name": table.get("NAME") or self._gauges.get("ROOM_NAME"),
            "area": (
                table.get("AREA") or self._gauges.get("ROOM_AREA")
                or self._gauges.get("AREA_NAME")
            ),
            "exits": table.get("EXITS") or self._gauges.get("ROOM_EXITS"),
            "vnum": table.get("VNUM") or self._gauges.get("ROOM_VNUM"),
        }
        return {key: value for key, value in room.items() if value}

    def _server_goodbye(self, reason: object) -> None:
        """GMCP ``Core.Goodbye``: the server is closing this link deliberately, and the body
        says why (kicked, banned, shutting down, logged in elsewhere). Suppress first: the
        drop can arrive before the speech does, and reconnecting into a ban helps nobody."""
        text = reason.strip() if isinstance(reason, str) else ""
        closed = "The MUD closed the connection"
        spoken = f"{closed}: {text}" if text else f"{closed}."
        if self._diag is not None:
            self._diag.event("gmcp.goodbye", reason=text)
        self._suppress_reconnect()
        self.voice.speak(spoken, channel=SYSTEM_CHANNEL, interrupt=True)
        self._post(protocol.echo(f"* {spoken}"))

    @staticmethod
    def _is_room_info(message: OobMessage) -> bool:
        return (
            message.source == "gmcp"
            and message.name.lower() == "room.info"
            and isinstance(message.value, dict)
        )
