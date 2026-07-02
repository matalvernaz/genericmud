"""Pack install/download hardening: zip-bomb quota (#13) and vault SSRF guard (#14)."""

from __future__ import annotations

import zipfile

import pytest

import genericmud.packs.store as store
from genericmud.packs.store import PackError, extract_pack
from genericmud.packs.vault import BlockedUrl, _validate_url, _ValidatingRedirectHandler


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


# --- #1: the zip quota bounds the whole nested tree, not each archive independently ---


def test_zip_quota_budget_accumulates_across_archives(tmp_path, monkeypatch):
    """Two archives, each under the per-archive cap, must not both pass one shared budget."""
    monkeypatch.setattr(store, "_MAX_PACK_TOTAL_BYTES", 2000)
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("f.bin", b"x" * 1500)  # 1500 < 2000: fine on its own
    budget = store._ExtractBudget(bytes_left=2000, members_left=store._MAX_PACK_MEMBERS)
    with zipfile.ZipFile(z) as arch:
        store._check_zip_quota(arch, budget)  # first draw: 1500 <= 2000
    with zipfile.ZipFile(z) as arch:
        with pytest.raises(PackError):
            store._check_zip_quota(arch, budget)  # cumulative 3000 > 2000 -> rejected


def test_extract_pack_rejects_a_nested_zip_tree_over_the_total(tmp_path, monkeypatch):
    """A wrapper of nested zips, each tiny on disk but large uncompressed, can't beat the cap."""
    monkeypatch.setattr(store, "_MAX_PACK_TOTAL_BYTES", 20_000)

    def inner(path):  # deflated zeros: a ~200-byte file whose member is 9000 bytes uncompressed
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\x00" * 9000)

    for name in ("a.zip", "b.zip", "c.zip"):
        inner(tmp_path / name)
    wrapper = tmp_path / "wrapper.zip"
    with zipfile.ZipFile(wrapper, "w", zipfile.ZIP_STORED) as zf:
        for name in ("a.zip", "b.zip", "c.zip"):
            zf.write(tmp_path / name, name)
    with pytest.raises(PackError):
        extract_pack(wrapper, tmp_path / "out")  # 3 x 9000 uncompressed > 20000 across the tree


def test_extract_pack_accepts_a_nested_zip_tree_under_the_total(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_MAX_PACK_TOTAL_BYTES", 20_000)
    sounds = tmp_path / "sounds.zip"
    with zipfile.ZipFile(sounds, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hit.wav", b"\x00" * 9000)
    wrapper = tmp_path / "wrapper.zip"
    with zipfile.ZipFile(wrapper, "w", zipfile.ZIP_STORED) as zf:
        zf.write(sounds, "sounds.zip")
        zf.writestr("main.set", "#TRIGGER hi {#say hi}")
    extract_pack(wrapper, tmp_path / "out")  # one 9000-byte member < 20000: fine
    assert (tmp_path / "out" / "main.set").is_file()
    assert (tmp_path / "out" / "sounds" / "hit.wav").is_file()  # nested zip was descended


# --- #2: the SSRF check re-runs on every redirect hop, not just the entry URL ---


def test_redirect_handler_blocks_a_private_hop():
    from email.message import Message
    from urllib.request import Request

    handler = _ValidatingRedirectHandler()
    req = Request("https://public.example/pack.zip")
    for evil in ("http://127.0.0.1/x.zip", "http://169.254.169.254/latest", "file:///etc/passwd"):
        with pytest.raises(BlockedUrl):
            handler.redirect_request(req, None, 302, "Found", Message(), evil)


def test_redirect_handler_allows_a_public_hop():
    from email.message import Message
    from urllib.request import Request

    handler = _ValidatingRedirectHandler()
    req = Request("https://public.example/pack.zip")
    new = handler.redirect_request(req, None, 302, "Found", Message(), "https://93.184.216.34/x.zip")
    assert new is not None  # a public redirect target is followed as normal
