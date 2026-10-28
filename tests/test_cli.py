"""Top-level launcher argument validation."""

from __future__ import annotations

import pytest

from genericmud.__main__ import _parse_args


def test_port_accepts_valid_tcp_port():
    assert _parse_args(["mud.example", "4000"]).port == 4000


@pytest.mark.parametrize("port", ("0", "65536", "not-a-port"))
def test_port_rejects_invalid_values(port):
    with pytest.raises(SystemExit):
        _parse_args(["mud.example", port])


def test_an_images_address_is_optional_and_web_only():
    # The browser-only mode has no saved world, so its pictures address comes from here.
    assert _parse_args(["mud.example"]).images is None
    assert _parse_args(["mud.example", "--images", "https://mud.example/art/"]).images == (
        "https://mud.example/art/"
    )
    with pytest.raises(SystemExit):
        _parse_args(["mud.example", "--images", "file:///C:/pictures"])
