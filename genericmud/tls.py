"""Secure connections check a server's certificate against the computer's own trusted list.

Every secure connection genericMud makes takes its context from :func:`client_context`: a MUD
on a TLS port, sound and soundpack downloads, the git client that updates packs, and the update
check.

Python's default context asks OpenSSL which certificates to trust, and OpenSSL looks where it
was told to when it was built. The standalone apps carry their own OpenSSL, and on a player's
Mac that place is empty: the Mac app's update check failed with "unable to get local issuer
certificate". truststore asks the operating system instead: the Keychain on a Mac, the
certificate store on Windows, and on Linux OpenSSL with the system's certificate bundle, looked
for where distributions keep it when OpenSSL's own place is empty. A browser with a certificate
list of its own, like Firefox, can still trust differently.
"""

from __future__ import annotations

import ssl

import truststore


def client_context() -> ssl.SSLContext:
    """A new context for connecting to a server: the server's certificate and host name are
    both checked, and the operating system decides which certificates are trusted."""
    # PROTOCOL_TLS_CLIENT turns on both checks (CERT_REQUIRED and check_hostname).
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
