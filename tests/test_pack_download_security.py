"""Pack install/download hardening: zip-bomb quota (#13) and vault SSRF guard (#14)."""

from __future__ import annotations

import zipfile

import pytest

from genericmud.packs.store import PackError, extract_pack
from genericmud.packs.vault import BlockedUrl, _validate_url


def test_extract_pack_accepts_a_normal_pack(tmp_path):
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.set", "#TRIGGER hi {#say hi}")
    extract_pack(src, tmp_path / "out")
    assert (tmp_path / "out" / "main.set").is_file()


def test_extract_pack_rejects_a_compression_bomb(tmp_path):
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"\x00" * (50 * 1024 * 1024))  # ~1000x ratio, well over the cap
    with pytest.raises(PackError):
        extract_pack(bomb, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # local file read
        "ftp://host/x.zip",  # non-web scheme
        "http://127.0.0.1/x.zip",  # loopback
        "http://10.0.0.5/x.zip",  # RFC1918 private
        "http://169.254.169.254/latest",  # link-local (cloud metadata shape)
    ],
)
def test_validate_url_blocks_ssrf(url):
    with pytest.raises(BlockedUrl):
        _validate_url(url)


def test_validate_url_allows_a_public_address():
    _validate_url("http://93.184.216.34/pack.zip")  # a public IP literal must not raise
