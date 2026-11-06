"""This world's rules, scripts, room map and saved soundpack variables."""

from __future__ import annotations

import json
from pathlib import Path

from genericmud.automation.engine import AutomationEngine
from genericmud.config.atomic import atomic_write_text
from genericmud.mapping import load_map, save_map
from genericmud.packs import user_rules
from genericmud.safepath import legacy_world_component, world_component
from genericmud.scripting import rule_scripts, user_scripts
from genericmud.scripting.api import ScriptApi
from genericmud.voice.router import SYSTEM_CHANNEL


class WorldFilesMixin:
    """This world's rules, scripts, room map and saved soundpack variables.

    Part of EngineApp (engine_app.py). Every attribute below starts in its constructor.

    Writes: _map_unsaved, _rule_scripts, _user_script_generation, _user_script_runtimes,
    map.

    Reads: _diag, _other_worlds, _vip_packs, _world_paths, engine, map_dir, name, packs, voice.

    Calls: nothing outside.

    _map_unsaved is shared: walking counts new rooms; world_files resets it after saving.
    """

    def user_rules_dir(self) -> Path | None:
        """This world's user-pack dir (rules, sounds, and scripts); None headless."""
        if self.packs is None or not self.name:
            return None
        root = getattr(self.packs, "root", None)
        if root is None:
            return None
        return self._world_path(Path(root).parent / "userpacks")

    def _world_path(self, root: Path, suffix: str = "") -> Path:
        """This world's file or folder under ``root``, found where older builds filed it.

        Names used to be filed ASCII-only, so "Café" went under "Caf". Where the current
        name has nothing yet and the old one does, the old one is still this world's: its
        rules and map carry on after an update instead of seeming to vanish. Unless another
        saved world is literally called "Caf": then that folder is its own, and adopting
        it would mix the two worlds' rules and maps. Resolved once per session, so a save
        at close lands where the load at connect read from.
        """
        key = (root, suffix)
        if key not in self._world_paths:
            self._world_paths[key] = self._resolve_world_path(root, suffix)
        return self._world_paths[key]

    def _resolve_world_path(self, root: Path, suffix: str) -> Path:
        current = root / f"{world_component(self.name)}{suffix}"
        old_name = legacy_world_component(self.name)
        if not old_name or current.exists():
            return current
        legacy = root / f"{old_name}{suffix}"
        if legacy == current or not legacy.exists() or self._owned_by_another_world(old_name):
            return current
        return legacy

    def _owned_by_another_world(self, component: str) -> bool:
        """Whether another saved world has a claim on the old folder ``component``.

        It does if it is filed under that name now, or if an older build filed it there
        too ("Café" and "Cafè" were both "Caf"). Deciding between claimants needs every
        world at once, which config/world_files.py does at launch by copying the folder
        to each; a session on its own leaves an ambiguous folder alone.
        """
        if self._other_worlds is None:
            return False
        own = self.name.casefold()
        folded = component.casefold()  # on NTFS and APFS "Caf" and "CAF" are one folder
        for name in self._other_worlds():
            if name.casefold() == own:
                continue
            claims = {world_component(name).casefold(), legacy_world_component(name).casefold()}
            if folded in claims:
                return True
        return False

    def reload_user_rules(self) -> None:
        """(Re)register the world's field-based automation; safe to call live after a save.

        Validates the new rules against a throwaway engine BEFORE removing the live ones, so a
        rule with a bad advanced-regex can't clear every user rule and leave the world silent
        with an invalid rules.json still on disk. On a validation failure the working set is kept
        and the error is spoken/traced instead of swallowed by the reload's threadsafe callback.
        """
        pack_dir = self.user_rules_dir()
        if pack_dir is None:
            return
        rules = user_rules.load_rules(pack_dir)
        try:
            user_rules.register_rules(
                ScriptApi(AutomationEngine(), source=user_rules.SOURCE, base_dir=str(pack_dir)),
                rules,
            )
        except Exception as error:  # noqa: BLE001 - a bad rule must not destroy the working set
            if self._diag is not None:
                self._diag.event(
                    "user_rules.reload_failed", error=f"{type(error).__name__}: {error}"
                )
            self.voice.speak(f"Rule not applied: {error}", channel=SYSTEM_CHANNEL, interrupt=False)
            return
        self.engine.remove_source(user_rules.SOURCE)
        api = ScriptApi(self.engine, source=user_rules.SOURCE, base_dir=str(pack_dir))
        # A rule's "send to command" puts text where Enter makes it the player's typing, so it
        # passes the same check a MUD's link does -- but only when a capture is what made it
        # unsafe, since the player's own text may say what it likes.
        api.command_text_refusal = self._rule_command_refusal
        # A rule's "send to script" reaches this world's Lua through the api it calls on.
        api.scripts = self.rule_scripts(pack_dir)
        user_rules.register_rules(api, rules)

    def _rule_command_refusal(self, typed: str, authored: str) -> str | None:
        """Why a rule's staged text may not go in the command box, or None.

        ``typed`` is what would land there, ``authored`` the same text with every capture left
        out. If the player's own words already do what is refused, it was meant.
        """
        from genericmud.app.links import typed_refusal

        refusal = typed_refusal(typed, self.command_separator)
        if refusal is None:
            return None
        if typed_refusal(authored, self.command_separator) is not None:
            return None
        return refusal

    def rule_scripts(self, pack_dir):
        """The runner a rule's "send to script" uses: this world's Lua, and its loaded scripts."""
        runner = getattr(self, "_rule_scripts", None)
        if runner is None or runner.base_dir != str(pack_dir):
            runner = rule_scripts.RuleScripts(
                self.engine, str(pack_dir), self._user_script_runtimes
            )
            self._rule_scripts = runner
        else:
            runner.set_scripts(self._user_script_runtimes)
        return runner

    def reload_user_scripts(self, announce: bool = False) -> bool:
        """Transactionally replace this world's ordered native-Lua automation scripts."""
        pack_dir = self.user_rules_dir()
        if pack_dir is None:
            return False
        next_generation = self._user_script_generation + 1
        prefix = f"{user_scripts.SOURCE_PREFIX}{next_generation}"
        try:
            loaded = user_scripts.load_scripts(self.engine, pack_dir, source_prefix=prefix)
        except Exception as error:  # noqa: BLE001 - keep the previous working script set live
            if self._diag is not None:
                self._diag.event(
                    "user_scripts.reload_failed", error=f"{type(error).__name__}: {error}"
                )
            self.voice.speak(
                f"Scripts not applied: {type(error).__name__}: {error}",
                channel=SYSTEM_CHANNEL, interrupt=False,
            )
            return False

        for item in self._user_script_runtimes:
            self.engine.remove_source(item.source)
        self._user_script_runtimes = loaded
        self._user_script_generation = next_generation
        # The runner a rule's "send to script" calls through is built by reload_user_rules,
        # which the app runs before this on connect. Hand it the scripts that just loaded, or a
        # rule sending "on_backup" runs that as code and speaks a Lua syntax error instead.
        runner = getattr(self, "_rule_scripts", None)
        if runner is not None:
            runner.set_scripts(loaded)
        if self._diag is not None:
            self._diag.event(
                "user_scripts.reloaded", count=len(loaded), generation=next_generation
            )
        if announce:
            count = len(loaded)
            self.voice.speak(
                f"{count} automation script{'s' if count != 1 else ''} loaded.",
                channel=SYSTEM_CHANNEL, interrupt=False,
            )
        return True

    def _map_path(self) -> Path | None:
        """This world's map file, or None for a session with no name to file it under."""
        if not self.name:
            return None
        return self._world_path(self.map_dir, ".json")

    def _load_map(self) -> None:
        path = self._map_path()
        if path is None:
            return
        self.map = load_map(path)
        self._map_unsaved = 0

    def _save_map(self) -> None:
        path = self._map_path()
        if path is None or not self.map.rooms:
            return
        if save_map(self.map, path):
            self._map_unsaved = 0

    def _pack_vars_path(self) -> Path | None:
        if self.packs is None or not self.name:
            return None  # headless/test sessions have nothing to persist against
        root = getattr(self.packs, "root", None)
        if root is None:
            return None
        return self._world_path(Path(root).parent / "state", "-vars.json")

    def _restore_pack_vars(self) -> None:
        path = self._pack_vars_path()
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # no state yet, or an unreadable file: packs fall back to defaults
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str) and key != "sppath":
                self.engine.set_var(key, value)

    def _persist_pack_vars(self) -> None:
        path = self._pack_vars_path()
        if path is None:
            return
        # sppath is wiring (where THIS install keeps its sounds), not a user setting;
        # persisting it would pin a moved/reinstalled pack to its old location. A VIPMud pack
        # that saves its settings in its own files starts each session without its variables.
        transient = {"sppath"}.union(*(
            pack.written_variables for pack in self._vip_packs if pack.saves_its_own_settings
        ))
        data = {k: v for k, v in self.engine.all_vars().items() if k not in transient}
        try:
            atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))
        except OSError:
            pass  # a state write must never break session close
