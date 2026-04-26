"""``polling_interval`` / ``polling_chunk`` constructor knobs propagate."""

from __future__ import annotations

from pymonik import PymonikClient


def test_polling_defaults():
    # No connection — just check the kwargs landed on the instance.
    c = PymonikClient(endpoint="grpcs://test:5001")
    assert c._polling_interval == 0.5
    assert c._polling_chunk == 500


def test_polling_overrides():
    c = PymonikClient(
        endpoint="grpcs://test:5001",
        polling_interval=2.0,
        polling_chunk=100,
    )
    assert c._polling_interval == 2.0
    assert c._polling_chunk == 100


def test_session_inherits_client_polling_settings(monkeypatch):
    """Session reads the polling kwargs from the client so a single
    ``PymonikClient(polling_interval=...)`` configures every session."""
    from pymonik.session import Session

    c = PymonikClient(
        endpoint="grpcs://test:5001",
        polling_interval=3.5,
        polling_chunk=42,
    )
    # Pure constructor; doesn't connect.
    s = Session(
        c,
        partition="x",
        polling_interval=c._polling_interval,
        polling_chunk=c._polling_chunk,
    )
    assert s._polling_interval == 3.5
    assert s._polling_chunk == 42
