"""Worker-side execution context.

``pymonik.current()`` returns the currently-executing task's context:
structured logger bound with task/session ids, attempt counter, partition,
and a cancellation check. User code calls this *inside* a @task function
to get at worker-side state without polluting the function signature.

Not available on the client; raises ``RuntimeError`` if called there.

Cancellation
------------
The gRPC server context passed into the worker's ``Process`` handler is
captured by ``worker.run`` (via a ``ContextVar``) and stashed here. When
ArmoniK's polling-agent cancels its outgoing gRPC call — the signal the
control plane sends on ``CancelTasks`` / ``CancelSession`` — the context
reports ``is_active() == False`` and
:meth:`WorkerContext.cancel_if_requested` raises :class:`TaskCancelled`.

Cooperation is on the user: long-running tasks have to call
``pymonik.current().cancel_if_requested()`` at a safe point. A task that
never calls it runs to ``max_duration`` regardless of cluster state.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any

from pymonik._internal._logging import get_logger
from pymonik.errors import TaskCancelled

if TYPE_CHECKING:
    from armonik.worker import TaskHandler


class WorkerContext:
    """What ``pymonik.current()`` returns inside a worker task."""

    __slots__ = ("_th", "_log", "attempt", "_grpc_context", "_cancel_check")

    def __init__(
        self,
        task_handler: "TaskHandler",
        *,
        attempt: int = 1,
        grpc_context: Any = None,
        cancel_check: Any = None,
    ) -> None:
        self._th = task_handler
        self.attempt = attempt
        self._grpc_context = grpc_context
        # Optional callable() -> bool. When set, takes precedence over
        # grpc_context.is_active() in :meth:`cancelled` — used by
        # ``LocalCluster`` to wire cancellation to a local threading.Event
        # rather than a real gRPC server context.
        self._cancel_check = cancel_check
        self._log = get_logger("pymonik.task").bind(
            task_id=task_handler.task_id,
            session_id=task_handler.session_id,
            attempt=attempt,
        )

    @property
    def log(self) -> Any:
        return self._log

    @property
    def task_id(self) -> str:
        return self._th.task_id

    @property
    def session_id(self) -> str:
        return self._th.session_id

    @property
    def task_handler(self) -> "TaskHandler":
        """Escape hatch for direct armonik TaskHandler access."""
        return self._th

    # ---- cancellation ----

    @property
    def cancelled(self) -> bool:
        """``True`` if cancellation has been signalled for this task.

        Non-raising — use inside a boolean condition. See
        :meth:`cancel_if_requested` for the raising form.
        """
        if self._cancel_check is not None:
            try:
                return bool(self._cancel_check())
            except Exception:  # pragma: no cover — defensive
                return False
        ctx = self._grpc_context
        if ctx is None:
            return False
        try:
            return not ctx.is_active()
        except Exception:  # pragma: no cover — defensive
            return False

    def cancel_if_requested(self) -> None:
        """Raise :class:`TaskCancelled` if cancellation has been signalled.

        Call from any point in your @task where it's safe to stop.
        """
        if self.cancelled:
            raise TaskCancelled(self._th.task_id)


# Public alias for the typed dependency-injection form:
#
#     @task
#     def render(scene: Scene, *, ctx: pymonik.Ctx) -> bytes:
#         ctx.log.info("rendering", id=ctx.task_id)
#
# A parameter annotated ``pymonik.Ctx`` (or ``WorkerContext``) is detected
# at decoration and the live context is injected by the worker at dispatch.
# Equivalent to calling ``pymonik.current()`` inside the body.
Ctx = WorkerContext


_current: contextvars.ContextVar[WorkerContext | None] = contextvars.ContextVar(
    "_pymonik_worker_ctx", default=None
)


def current() -> WorkerContext:
    """Return the context for the currently-executing task.

    Raises:
        RuntimeError: if called outside a @task function (e.g. from client code).
    """
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError(
            "pymonik.current() called outside a worker task. "
            "It is only meaningful inside a @task function running on a worker."
        )
    return ctx


def _set(ctx: WorkerContext):
    """Internal: set the current context and return the token."""
    return _current.set(ctx)


def _reset(token) -> None:
    _current.reset(token)
