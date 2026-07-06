"""Internal protocols.

Capture the duck-typed contracts used across the codebase so static
checkers can see through them — the alternative is ``Any``-typed
``ContextVar`` slots, which both lie about what's reachable and hide
typos.

``SubmittableSession`` is the union of what ``Task.spawn`` /
``Task.map`` / ``blob.upload`` need from whatever's stored in the
``_current_session`` ContextVar. Three concrete implementations:

- :class:`pymonik.session.Session` — control-plane gRPC.
- :class:`pymonik.worker_session.WorkerSession` — agent-sidecar gRPC.
- :class:`pymonik.testing.local.LocalSession` — in-process executor.

All three duck-type cleanly; the Protocol just makes that fact
explicit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pymonik.future import Future, FutureList
    from pymonik.task import Task


class SubmittableSession(Protocol):
    """Minimum contract that ``Task.spawn`` / ``blob.upload`` rely on."""

    @property
    def session_id(self) -> str: ...

    def _submit_one(
        self,
        task: "Task[Any, Any]",
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> "Future[Any]": ...

    def _submit_many(
        self,
        task: "Task[Any, Any]",
        calls: list[Any],
    ) -> "FutureList[Any]": ...

    def _upload_blob(self, data: bytes) -> str: ...
