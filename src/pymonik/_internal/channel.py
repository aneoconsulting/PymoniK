"""gRPC channel construction. Insecure by default; TLS optional.

Wraps the upstream armonik helper so callers get one place to configure auth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import grpc
from armonik.common import create_channel as _armonik_create_channel


@dataclass(frozen=True, slots=True)
class Credentials:
    """mTLS credentials — all three fields required when any is given."""

    ca: Optional[str] = None
    cert: Optional[str] = None
    key: Optional[str] = None

    @property
    def tls(self) -> bool:
        return any((self.ca, self.cert, self.key))


def _strip_scheme(endpoint: str) -> str:
    for scheme in ("https://", "http://", "grpcs://", "grpc://"):
        if endpoint.startswith(scheme):
            endpoint = endpoint[len(scheme) :]
    return endpoint.rstrip("/")


def open_channel(endpoint: str, credentials: Optional[Credentials] = None) -> grpc.Channel:
    """Open a sync gRPC channel. Callers are responsible for closing it."""
    endpoint = _strip_scheme(endpoint)
    if credentials and credentials.tls:
        return _armonik_create_channel(
            endpoint,
            certificate_authority=credentials.ca,
            client_certificate=credentials.cert,
            client_key=credentials.key,
        )
    return grpc.insecure_channel(endpoint)
