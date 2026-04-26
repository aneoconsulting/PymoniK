"""``client.session(attach_to=session_id)`` — pick up an existing session.

The attach path doesn't issue ``create_session`` and doesn't issue
``close_session`` on exit. We don't have a real cluster in unit tests,
so these checks operate on a `Session` constructed with a stubbed
client/channel — enough to verify the contract that:

- ``_open_resources`` skips ``create_session`` and uses the supplied
  id verbatim.
- ``_close_resources`` skips ``close_session``.
- ``Session.session_id`` returns the attached id.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pymonik.session import Session


class _Channel:
    def close(self):
        pass


class _StubClient:
    def __init__(self):
        self._channel = _Channel()


@pytest.fixture
def stub_session(monkeypatch):
    """Patch the armonik client classes the Session constructs at open.

    Returns ``(session, sessions_mock)`` so tests can assert on RPC calls.
    """
    from pymonik import session as session_mod

    sessions_mock = MagicMock()
    sessions_mock.create_session = MagicMock(
        return_value="created-fresh-id"
    )
    sessions_mock.close_session = MagicMock()

    monkeypatch.setattr(session_mod, "ArmoniKSessions", lambda _ch: sessions_mock)
    monkeypatch.setattr(session_mod, "ArmoniKTasks", lambda _ch: MagicMock())
    monkeypatch.setattr(session_mod, "ArmoniKResults", lambda _ch: MagicMock())
    monkeypatch.setattr(session_mod, "ArmoniKEvents", lambda _ch: MagicMock())

    yield sessions_mock


def test_attach_skips_create_and_close(stub_session):
    """The attach path doesn't create or close the session."""
    sess = Session(
        client=_StubClient(),  # type: ignore[arg-type]
        partition="pymonik",
        attach_to="existing-session-abc",
        use_events=False,  # avoid needing a real events stream
    )

    sess._open_resources()
    try:
        assert sess.session_id == "existing-session-abc"
        stub_session.create_session.assert_not_called()
    finally:
        sess._stop.set()
        sess._close_resources()

    stub_session.close_session.assert_not_called()


def test_create_path_unchanged(stub_session):
    """Without attach_to, behaviour is unchanged: create on open, close on exit."""
    sess = Session(
        client=_StubClient(),  # type: ignore[arg-type]
        partition="pymonik",
        use_events=False,
    )

    sess._open_resources()
    try:
        assert sess.session_id == "created-fresh-id"
        stub_session.create_session.assert_called_once()
    finally:
        sess._stop.set()
        sess._close_resources()

    stub_session.close_session.assert_called_once_with("created-fresh-id")


def test_attach_keeps_partition_validation(stub_session):
    """Per-task partition validation still runs against the supplied
    partition list (the cluster-side declaration was at create time;
    we trust the user's list for client-side checks)."""
    sess = Session(
        client=_StubClient(),  # type: ignore[arg-type]
        partition=["pymonik", "gpu"],
        attach_to="existing-session-xyz",
        use_events=False,
    )
    assert sess.partitions == ("pymonik", "gpu")
    assert sess.partition == "pymonik"


def test_client_session_passes_attach_to(stub_session):
    """``client.session(attach_to=...)`` propagates to the Session."""
    from pymonik import PymonikClient

    client = PymonikClient(endpoint="grpcs://test:5001")
    # Build the session without entering the client (no real channel).
    client._channel = _Channel()  # type: ignore[assignment]
    sess = client.session(partition="pymonik", attach_to="abc-123")
    assert sess._attach_to == "abc-123"
