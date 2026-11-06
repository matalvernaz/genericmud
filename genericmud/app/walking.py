"""Speedwalks, room reports, map destinations and breadcrumb navigation."""

from __future__ import annotations

from genericmud.mapping import spoken_count
from genericmud.navigation import SafeWalk, expand_speedwalk

SPEEDWALK_PREFIX = "."  # ".3n2e" expands to n,n,n,e,e (leading char disambiguates)
SAFE_PREFIX = ".."  # "..3n2e" walks the same route one step at a time, halting if blocked


# New rooms learned before the map is written again. Exploring is exactly when a session
# is long and a crash is most costly, and a write every twenty-five rooms is nothing.
_MAP_SAVE_INTERVAL = 25


class WalkingMixin:
    """Speedwalks, room reports, map destinations and breadcrumb navigation.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: _map_unsaved, _walk.

    Reads: _schedule, _send, follow_mode, map, nav, voice.

    Calls: _speak_system, world_files (_save_map).

    _map_unsaved is shared: walking counts new rooms; world_files resets it after saving.
    """

    def _wrong_direction(self, direction: object) -> None:
        """GMCP ``room.wrongdir``: the MUD says that exit doesn't exist. Stop walking.

        Authoritative where the blocked-movement line patterns are an English guess, so a
        route built from a stale edge halts here instead of firing its remaining steps.
        The exit is deliberately NOT removed from the map: a closed door and a door that
        was never there look identical from here, and dropping a real exit loses a route.
        """
        if self._walk is None or not self._walk.active:
            return
        self._walk.cancel()
        name = str(direction).strip() if direction else ""
        self._speak_system(f"no exit {name}, walk stopped" if name else "walk stopped, no exit")

    def _update_room(self, room: dict) -> None:
        changed = room != self.nav.room
        self.nav.update_room(room)
        was = self.map.here
        known = len(self.map.rooms)
        room_id = self.map.observe(room)
        if len(self.map.rooms) > known:
            self._map_unsaved += 1
            if self._map_unsaved >= _MAP_SAVE_INTERVAL:
                self._save_map()  # a crash mustn't cost a whole session's exploring
        # A MUD that identifies its rooms gives an exact movement signal. Comparing whole
        # reports instead counts a re-send of the SAME room (an exit list that grew, a
        # detail that changed) as a move, which advances a walk the player hasn't taken a
        # step of. Only reports carrying no id fall back to comparing the payload.
        moved = room_id != was if room_id else changed
        if moved:
            self._on_player_moved()  # follow mode: the new room barges over the old one
        if moved and self._walk is not None and self._walk.active:
            self._walk.on_room_change()  # confirmed move -> advance the safe-walk

    def _on_player_moved(self) -> None:
        """Movement signal (a direction sent, or GMCP room change) for follow mode.

        Cuts the current utterance so the next room reads immediately; ordinary
        lines keep queueing. Both signals may fire for one move — stop is idempotent.
        """
        if self.follow_mode:
            self.voice.interrupt()

    def _safe_speedwalk(self, text: str) -> bool:
        """Walk a "..3n2e" run step-by-step, halting if blocked; False if not one."""
        if not text.startswith(SAFE_PREFIX):
            return False
        steps = expand_speedwalk(text[len(SAFE_PREFIX) :])
        if not steps:
            return False
        self._start_safe_walk(steps)
        return True

    def _start_safe_walk(
        self, steps: list[str], announcement: str = "", waypoints: list[str] | None = None
    ) -> None:
        """Begin a step-at-a-time walk, superseding any walk already running.

        ``waypoints`` (a mapped route only) is the room each step should land in, so the
        walk halts if something moves the player off it.
        """
        self._cancel_walk()  # don't leave both walks' timers firing into each other

        def send_and_record(direction: str) -> None:
            self._send(direction)
            if self.nav.record(direction):
                self._on_player_moved()

        self._walk = SafeWalk(
            steps,
            send=send_and_record,
            schedule=self._schedule,
            announce=self._speak_system,
            waypoints=waypoints,
            locate=lambda: self.map.here,
            reports_rooms=self.nav.room is not None,
        )
        if announcement:
            self._speak_system(announcement)
        self._walk.start()

    def _cancel_walk(self) -> None:
        if self._walk is not None and self._walk.active:
            self._walk.cancel()

    def _speedwalk(self, text: str) -> bool:
        """Expand and send a "." speedwalk run (e.g. ".3n2e"); False if not one."""
        if not text.startswith(SPEEDWALK_PREFIX):
            return False
        steps = expand_speedwalk(text[len(SPEEDWALK_PREFIX) :])
        if not steps:
            return False
        self._cancel_walk()  # an immediate speedwalk supersedes a step-by-step one in progress
        self._on_player_moved()  # one interrupt for the whole burst, not one per step
        for direction in steps:
            self._send(direction)
            self.nav.record(direction)
        return True


    def _goto(self, query: str) -> None:
        """Walk to a mapped room named by ``query``, step by step so it can halt."""
        if not query:
            self._speak_system("usage: /goto <room name>")
            return
        if not self.map.rooms:
            self._speak_system("no map yet; this MUD may not tell the client where you are")
            return
        if not self.map.here:
            self._speak_system("this room is not on the map, so there is nowhere to walk from")
            return
        matches = self.map.search(query)
        if not matches:
            self._speak_system(f"no mapped room matches {query}")
            return
        for room in matches:
            route = self.map.route_to(room.id)
            if route is None:
                continue
            path, waypoints = route
            target = room.label or room.name
            if not path:
                self._speak_system(f"you are already at {target}")
                return
            # Through SafeWalk, not a burst: it halts on a blocked exit, and a route
            # assembled from remembered rooms is exactly where a stale edge shows up.
            self._start_safe_walk(
                path, f"walking to {target}, {spoken_count(len(path), 'step')}", waypoints=waypoints
            )
            return
        # Every match is somewhere the map knows of but can't currently reach.
        found = matches[0].label or matches[0].name
        self._speak_system(f"{found} is on the map but no known route leads there from here")

    def _label_room(self, text: str) -> None:
        """Name the current room so /goto can find it later."""
        if not text:
            self._speak_system("usage: /label <name for this room>")
            return
        if not self.map.set_label(text):
            self._speak_system("this room is not on the map, so it cannot be labelled")
            return
        self._save_map()  # a label is deliberate work; don't risk losing it to a crash
        self._speak_system(f"this room is now {text}")

    def _handle_nav(self, action: str) -> None:
        if action == "mark":
            self.nav.clear()
            self._speak_system("breadcrumb dropped")
        elif action == "retrace":
            path = self.nav.retrace()
            if not path:
                self._speak_system("no trail to retrace")
                return
            for direction in path:
                self._send(direction)
            self.nav.clear()  # optimistic: assume the way back succeeded
            self._speak_system(f"retracing {len(path)} steps")
        elif action == "where":
            self._speak_system(self.nav.where())
        elif action == "stop":
            if self._walk is not None and self._walk.active:
                self._walk.cancel()
                self._speak_system("walk stopped")
            else:
                self._speak_system("not walking")
