"""Scraping the mudsoundpack.com catalogue + streaming a download (fake opener)."""

from __future__ import annotations

import io

import pytest

from genericmud.packs.vault import (
    BASE_URL,
    DownloadTooLarge,
    best_download,
    download,
    git_archive_urls,
    installer_source,
    list_packs,
    pack_downloads,
)

# Trimmed real /packs.php structure: header row + a Mush pack and a Mudlet pack.
PACKS_HTML = """<table>
<thead><tr><th>Pack</th><th>MUD</th><th>Client</th><th>Status</th></tr></thead>
<tbody>
<tr>
<td><a href="/pack.php?id=14">toastush</a><br><span class="meta">3.1.7</span></td>
<td><a href="https://toastsoft.net/" rel="nofollow">Miriani</a></td>
<td>Mush</td>
<td>archived</td>
</tr>
<tr>
<td><a href="/pack.php?id=17">The Mudlet Immersion</a><br><span class="meta"></span></td>
<td><a href="https://cosmicrage.earth" rel="nofollow">cosmic Rage</a></td>
<td>Mudlet</td>
<td>active</td>
</tr>
</tbody></table>"""

PACK14_HTML = """<ul>
<li>Official URL: <a href="https://github.com/PsudoDeSudo/toastush" rel="nofollow">x</a></li>
<li>Vault mirror: <a href="https://mirror.example/toastush-v3-1-0.zip" rel="nofollow">x</a></li>
<li>Source page: <a href="https://miriani.toastsoft.net/soundpacks/Toastush">x</a></li>
</ul>"""

EXE_ONLY_HTML = '<li>Vault mirror: <a href="https://x.example/installer.exe">x</a></li>'


class _FakeResponse:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size=-1):
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _opener(pages: dict[str, bytes]):
    def open_(request):
        return _FakeResponse(pages[request.full_url])

    return open_


def test_list_packs_parses_rows_and_marks_supported():
    packs = list_packs(opener=_opener({f"{BASE_URL}/packs.php": PACKS_HTML.encode()}))
    assert [p.id for p in packs] == [14, 17]
    toast = packs[0]
    assert (toast.name, toast.mud, toast.client, toast.version, toast.status) == (
        "toastush", "Miriani", "Mush", "3.1.7", "archived",
    )
    assert toast.supported  # Mush
    assert not packs[1].supported  # Mudlet is listed but not loadable


def test_pack_downloads_best_is_the_zip_mirror():
    dls = pack_downloads(14, opener=_opener({f"{BASE_URL}/pack.php?id=14": PACK14_HTML.encode()}))
    assert [d.role for d in dls] == ["mirror", "official", "source"]
    best = best_download(dls)
    assert best.role == "mirror" and best.kind == "zip"
    assert best.url.endswith("toastush-v3-1-0.zip")


def test_exe_mirror_is_not_installable():
    dls = pack_downloads(26, opener=_opener({f"{BASE_URL}/pack.php?id=26": EXE_ONLY_HTML.encode()}))
    assert dls[0].kind == "exe" and not dls[0].installable
    assert best_download(dls) is None


def test_download_streams_to_file_with_progress(tmp_path):
    data = b"hello sound" * 500
    seen: list[tuple[int, int]] = []
    out = download(
        "https://h.example/p.zip", tmp_path / "p.zip",
        opener=_opener({"https://h.example/p.zip": data}),
        progress=lambda done, total: seen.append((done, total)),
    )
    assert out.read_bytes() == data
    assert seen[-1] == (len(data), len(data))


def test_download_aborts_past_the_size_cap(tmp_path):
    data = b"x" * 5000
    with pytest.raises(DownloadTooLarge):
        download(
            "https://h.example/big.zip", tmp_path / "big.zip",
            opener=_opener({"https://h.example/big.zip": data}), max_bytes=1000,
        )


def test_installer_source_finds_the_git_clone(tmp_path):
    (tmp_path / "installer.bat").write_text(
        "@echo off\nstart/wait PortableGit\\bin\\git.exe clone "
        "https://gitlab.com/erion1/soundpack.git soundpack\nexit",
        encoding="utf-8",
    )
    assert installer_source(tmp_path) == "https://gitlab.com/erion1/soundpack.git"


def test_installer_source_finds_a_repo_url_var(tmp_path):
    (tmp_path / "updator.bat").write_text(
        'set "SOUNDS_REPO_URL=https://nathantech.net:3000/CosmicRage/CosmicRageSounds.git"',
        encoding="utf-8",
    )
    assert installer_source(tmp_path).endswith("CosmicRageSounds.git")


def test_installer_source_ignores_bundled_git_tooling(tmp_path):
    docs = tmp_path / "PortableGit" / "doc"
    docs.mkdir(parents=True)
    (docs / "example.sh").write_text("git clone https://github.com/git/git.git", encoding="utf-8")
    assert installer_source(tmp_path) is None


def test_git_archive_urls_per_host():
    assert git_archive_urls("https://gitlab.com/erion1/soundpack.git")[0] == (
        "https://gitlab.com/erion1/soundpack/-/archive/master/soundpack-master.zip"
    )
    assert git_archive_urls("https://github.com/o/r.git")[0] == (
        "https://github.com/o/r/archive/refs/heads/master.zip"
    )
    assert git_archive_urls("https://nathantech.net:3000/CosmicRage/CosmicRageSounds.git")[0] == (
        "https://nathantech.net:3000/CosmicRage/CosmicRageSounds/archive/master.zip"
    )
