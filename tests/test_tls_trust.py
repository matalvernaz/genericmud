"""Secure connections trust the computer's certificates, not the folder OpenSSL was built with.

The standalone Mac app's own OpenSSL finds no trusted certificates on a player's Mac, and its
update check failed there with "unable to get local issuer certificate"; every other secure
connection took the same default. The ``frozen_openssl`` tests recreate that on Linux: OpenSSL's
own certificate file and folder point at nothing, and the system bundle that truststore falls back
to holds a test certificate authority. Each path the app takes has to connect anyway.

The other tests check that a certificate the computer doesn't trust, or one made out to another
host, is still refused. One of them runs on every platform, so the Mac and Windows builds
exercise truststore's own Keychain and certificate store code.
"""

from __future__ import annotations

import asyncio
import http.server
import ssl
import sys
import threading
import urllib.error
from pathlib import Path

import pytest
import trustme

from genericmud.protocol import telnet as T
from genericmud.sound.media import http_fetch, media_url
from genericmud.transport.connection import MudConnection
from genericmud.update import self_update

_SOUND = (Path(__file__).parent / "data" / "sounds" / "tone-8bit.wav").read_bytes()
_RELEASES = b'[{"tag_name": "v9.9.9"}]'
_GREETING = b"Bienvenido a la prueba.\r\n"

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="recreates the Mac app's missing certificates through truststore's Linux fallback",
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """The update check's release list, and one sound where a Crisol-style cue points."""

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        if self.path.startswith("/releases"):
            body, kind = _RELEASES, "application/json"
        elif self.path == "/sonidos/crisol/golpe.wav":
            body, kind = _SOUND, "audio/wav"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _HTTPSServer(http.server.ThreadingHTTPServer):
    """Each connection's handshake runs in its request thread, so a client that refuses the
    certificate can't stall the accept loop (and with it the fixture's shutdown)."""

    daemon_threads = True

    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.context = context

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(10)
        wrapped = self.context.wrap_socket(
            connection, server_side=True, do_handshake_on_connect=False
        )
        return wrapped, address

    def handle_error(self, request, client_address) -> None:
        pass  # a refused handshake is what several tests are for


def _server_context(authority: trustme.CA, host: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert(host).configure_cert(context)
    return context


@pytest.fixture
def authority() -> trustme.CA:
    """The certificate authority the computer trusts in the frozen_openssl tests."""
    return trustme.CA()


@pytest.fixture
def frozen_openssl(tmp_path, monkeypatch, authority):
    """OpenSSL with no certificates of its own, as in the standalone Mac app, and a system
    certificate bundle that trusts ``authority``.

    SSL_CERT_FILE and SSL_CERT_DIR replace the paths OpenSSL was built with, and pointing them
    at nothing is what the app's bundled OpenSSL meets on a player's Mac. truststore's Linux side
    then reads the bundle where Linux distributions keep it; that list is patched to hold the
    test authority, standing in for the Keychain its Mac side asks.
    """
    import truststore._openssl

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "no-such-file.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "no-such-folder"))
    bundle = tmp_path / "system-bundle.pem"
    authority.cert_pem.write_to_path(str(bundle))
    monkeypatch.setattr(truststore._openssl, "_CA_FILE_CANDIDATES", [str(bundle)])
    # The model has to hold: OpenSSL's own defaults now find no certificates at all.
    paths = ssl.get_default_verify_paths()
    assert paths.cafile is None
    assert paths.capath is None
    return bundle


@pytest.fixture
def serve_https():
    """``serve_https(authority, host)`` starts a local HTTPS server whose certificate
    ``authority`` made out to ``host``, and gives its address."""
    running = []

    def start(authority: trustme.CA, host: str = "127.0.0.1") -> str:
        server = _HTTPSServer(_server_context(authority, host))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return f"https://127.0.0.1:{server.server_address[1]}"

    yield start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def _update_check(address: str, tmp_path: Path) -> None:
    assert self_update._get_json(address + "/releases") == [{"tag_name": "v9.9.9"}]


def _update_download(address: str, tmp_path: Path) -> None:
    # The Windows updater's own downloader, separate from the check; any file serves here.
    destination = tmp_path / "update.zip"
    self_update._download(
        address + "/sonidos/crisol/golpe.wav", destination, expected_size=len(_SOUND)
    )
    assert destination.read_bytes() == _SOUND


def _sound_download(address: str, tmp_path: Path) -> None:
    # The shape of Crisol's cue: U= is the sounds folder, the name carries a subfolder.
    url = media_url(address + "/sonidos/", "crisol/golpe.wav")
    destination = tmp_path / "golpe.wav"
    http_fetch(url, destination, trusted_address="127.0.0.1")
    assert destination.read_bytes() == _SOUND


_HTTPS_PATHS = [
    pytest.param(_update_check, id="update-check"),
    pytest.param(_update_download, id="update-download"),
    pytest.param(_sound_download, id="sound-download"),
]


def _refused_certificate(error: BaseException) -> bool:
    """Whether ``error`` is, or wraps, a refused certificate (urllib wraps it in URLError)."""
    while error is not None:
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        reason = getattr(error, "reason", None)
        error = reason if isinstance(reason, BaseException) else error.__cause__
    return False


async def _connect_to_tls_mud(context: ssl.SSLContext) -> list[T.Event]:
    """Connect a MudConnection with TLS to a local MUD presenting ``context``'s certificate, and
    give back what it received; a refused certificate raises out of here."""
    received: list[T.Event] = []

    async def greet(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(_GREETING)
        await writer.drain()
        try:
            await reader.read()  # until the client hangs up
        except (ConnectionError, ssl.SSLError):
            pass
        writer.close()

    server = await asyncio.start_server(
        greet, "127.0.0.1", 0, ssl=context, ssl_handshake_timeout=10
    )
    port = server.sockets[0].getsockname()[1]
    connection = MudConnection(on_event=received.append)
    try:
        await asyncio.wait_for(connection.connect("127.0.0.1", port, tls=True), timeout=10)
        await _wait_for(
            lambda: any(
                isinstance(event, T.DataReceived) and _GREETING in event.data
                for event in received
            )
        )
    finally:
        await asyncio.wait_for(connection.close(), timeout=10)
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=10)
    return received


# What was broken on the Mac: every path connects with OpenSSL's own certificates missing.


@linux_only
@pytest.mark.parametrize("reach", _HTTPS_PATHS)
def test_https_works_without_openssls_own_certificates(
    frozen_openssl, authority, serve_https, tmp_path, reach
):
    reach(serve_https(authority), tmp_path)


@linux_only
async def test_a_tls_mud_connects_without_openssls_own_certificates(frozen_openssl, authority):
    received = await _connect_to_tls_mud(_server_context(authority, "127.0.0.1"))
    text = b"".join(event.data for event in received if isinstance(event, T.DataReceived))
    assert _GREETING in text


# Still refused: the system's trust decides, and it still says no.


@linux_only
@pytest.mark.parametrize("reach", _HTTPS_PATHS)
def test_https_refuses_a_certificate_made_out_to_another_host(
    frozen_openssl, authority, serve_https, tmp_path, reach
):
    address = serve_https(authority, "example.org")
    with pytest.raises(urllib.error.URLError) as caught:
        reach(address, tmp_path)
    assert _refused_certificate(caught.value), caught.value


@linux_only
async def test_a_tls_mud_with_a_certificate_for_another_host_is_refused(
    frozen_openssl, authority
):
    with pytest.raises(ssl.SSLCertVerificationError):
        await _connect_to_tls_mud(_server_context(authority, "example.org"))


@linux_only
@pytest.mark.parametrize("reach", _HTTPS_PATHS)
def test_https_refuses_an_authority_the_system_does_not_trust(
    frozen_openssl, serve_https, tmp_path, reach
):
    stranger = trustme.CA()  # never written to the system bundle
    with pytest.raises(urllib.error.URLError) as caught:
        reach(serve_https(stranger), tmp_path)
    assert _refused_certificate(caught.value), caught.value


@linux_only
async def test_a_tls_mud_from_an_authority_the_system_does_not_trust_is_refused(frozen_openssl):
    stranger = trustme.CA()
    with pytest.raises(ssl.SSLCertVerificationError):
        await _connect_to_tls_mud(_server_context(stranger, "127.0.0.1"))


@pytest.mark.parametrize("reach", _HTTPS_PATHS)
def test_every_platform_refuses_an_authority_the_computer_does_not_trust(
    serve_https, tmp_path, reach
):
    # No simulation here: on the Mac and Windows builds this is truststore asking the Keychain
    # or the Windows certificate store, which have never heard of a fresh test authority.
    with pytest.raises(urllib.error.URLError) as caught:
        reach(serve_https(trustme.CA()), tmp_path)
    assert _refused_certificate(caught.value), caught.value


# What the player hears when a certificate is refused.


async def test_a_refused_certificate_is_said_in_words(monkeypatch):
    # On a Mac or Windows the refusal is truststore's SSLCertVerificationError carrying the
    # system's description as its one argument (seen on the real Mac app against badssl.com),
    # which an SSLError prints as a tuple, brackets and quotes read out on every retry.
    description = "“mud.example” certificate is not trusted"

    async def refuse(host, port, **options):
        raise ssl.SSLCertVerificationError(description)

    monkeypatch.setattr(asyncio, "open_connection", refuse)
    connection = MudConnection()
    statuses: list[str] = []
    connection.on_status = statuses.append
    connection.configure_reconnect(True, 5)
    with pytest.raises(ssl.SSLCertVerificationError) as caught:
        await asyncio.wait_for(connection.connect("mud.example", 4000, tls=True), timeout=10)
    try:
        # The window says and speaks "Connect failed: {error}", then passes str(error) on as
        # the reason the retries announce.
        assert str(caught.value) == description
        assert connection.start_reconnect(detail=str(caught.value))
        assert await _wait_for(lambda: statuses)
        assert statuses[0] == f"connection unavailable: {description}; reconnecting every 5s"
    finally:
        await asyncio.wait_for(connection.close(), timeout=10)
