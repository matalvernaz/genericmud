"""Updater preference persistence, snooze, and skip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genericmud.config.update_prefs import (
    SNOOZE_DURATION,
    UpdatePrefs,
    is_snoozed,
    load_prefs,
    save_prefs,
    snooze_timestamp,
)


def test_prefs_roundtrip(tmp_path):
    path = tmp_path / "update-prefs.toml"
    prefs = UpdatePrefs(
        check_enabled=False,
        snoozed_until="2030-01-01T00:00:00+00:00",
        skipped_version="v1.2.3",
        last_check="2026-01-01T00:00:00+00:00",
    )
    save_prefs(prefs, path)
    assert load_prefs(path) == prefs


def test_defaults_when_missing(tmp_path):
    prefs = load_prefs(tmp_path / "nope.toml")
    assert prefs.check_enabled is True
    assert prefs.snoozed_until is None
    assert prefs.skipped_version is None


def test_is_snoozed():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    soon = UpdatePrefs(snoozed_until=(now + timedelta(days=1)).isoformat())
    past = UpdatePrefs(snoozed_until=(now - timedelta(days=1)).isoformat())
    assert is_snoozed(soon, now) is True
    assert is_snoozed(past, now) is False
    assert is_snoozed(UpdatePrefs(), now) is False
    # A corrupt timestamp must never permanently suppress prompts.
    assert is_snoozed(UpdatePrefs(snoozed_until="garbage"), now) is False


def test_snooze_timestamp():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert datetime.fromisoformat(snooze_timestamp(now)) == now + SNOOZE_DURATION
