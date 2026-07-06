"""Client-side lifecycle hooks — a public, typed observability surface.

Register a callback and PymoniK calls it (synchronously, in-process)
when something happens client-side: a session opens, tasks are
submitted, a future resolves or fails or retries.

    from pymonik import hooks

    @hooks.on(hooks.TaskFailed)
    def alert(ev: hooks.TaskFailed) -> None:
        print(ev.task_id, ev.error_type)   # cheap; offload if heavy

    unsub = hooks.subscribe(lambda ev: ...)  # all events; returns disposer
    unsub()

Contract (load-bearing — read before writing a hook):

- **Synchronous, on the publishing thread.** Hooks run on whatever
  thread reached the lifecycle point (the events-stream thread, a
  worker thread, the submitting thread). Do the minimum and return;
  offload real work to your own queue. A blocking hook stalls task
  resolution for *every* task.
- **Exceptions are isolated.** A hook that raises is caught, logged at
  ``debug``, and the next hook still runs — a buggy consumer can't fail
  a task.
- **Live stream, not a log.** Fire-and-forget, no buffering or replay;
  a hook registered after an event fired does not see it. Seed
  authoritative state from the introspection API if you need history.
- **Client-side only.** Tasks a *worker* spawns (``.starmap`` /
  ``.tail()`` from inside a ``@task``) emit on the worker's process,
  not here. Use ``session.tasks`` to observe those.

Cost when unused is ~nil: ``emit`` reads one immutable tuple, sees it's
empty, and returns without constructing an event.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import structlog

log = structlog.get_logger(__name__)


# ---------- events ----------


@dataclass(slots=True, frozen=True, kw_only=True)
class PymonikEvent:
    """Base for every hook event. ``at`` is a ``time.monotonic`` stamp —
    diff two events (e.g. ``TaskSubmitted`` → ``TaskCompleted`` for the
    same ``task_id``) to get an elapsed time."""

    session_id: str
    at: float = field(default_factory=time.monotonic)


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionOpened(PymonikEvent):
    partitions: tuple[str, ...] = ()
    attached: bool = False


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionClosed(PymonikEvent):
    cancelled: bool = False


@dataclass(slots=True, frozen=True, kw_only=True)
class TaskSubmitted(PymonikEvent):
    task_id: str
    task_name: str
    result_ids: tuple[str, ...] = ()
    # For a multi-output task, the field names in the SAME order as
    # ``result_ids`` (so result_ids[i] is the output of field multi_fields[i]).
    # Empty for single-output tasks. Lets a consumer label which field of a
    # MultiResult a downstream task depends on.
    multi_fields: tuple[str, ...] = ()
    data_dependencies: tuple[str, ...] = ()
    partition: str | None = None
    attempt: int = 1
    # Parent task id when submitted from inside a running ``@task`` body
    # (a worker subtask). ``None`` for ordinary client-side submissions.
    created_by: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class TaskCompleted(PymonikEvent):
    task_id: str
    result_id: str


@dataclass(slots=True, frozen=True, kw_only=True)
class TaskFailed(PymonikEvent):
    task_id: str
    result_id: str
    error_type: str
    message: str


@dataclass(slots=True, frozen=True, kw_only=True)
class TaskRetried(PymonikEvent):
    task_id: str
    attempt: int


E = TypeVar("E", bound=PymonikEvent)
Hook = Callable[[PymonikEvent], None]


# ---------- registry (copy-on-write; lock-free reads) ----------

# Subscribers live in one immutable tuple. Writers (subscribe/unsubscribe,
# both rare) replace the whole tuple under the lock; ``emit`` reads the
# reference into a local (atomic under the GIL) and never locks. See
_lock = threading.Lock()
_subscribers: tuple[Hook, ...] = ()


def subscribe(callback: Hook) -> Callable[[], None]:
    """Register ``callback`` for every event. Returns an idempotent
    disposer that unregisters it."""
    global _subscribers
    with _lock:
        _subscribers = (*_subscribers, callback)

    def _dispose() -> None:
        unsubscribe(callback)

    return _dispose


def unsubscribe(callback: Hook) -> None:
    """Remove ``callback``. No-op if it isn't registered."""
    global _subscribers
    with _lock:
        _subscribers = tuple(c for c in _subscribers if c is not callback)


def on(
    event_type: type[E],
    callback: Callable[[E], None] | None = None,
):
    """Register a callback for one event type. Call form returns a
    disposer; decorator form returns the function unchanged::

        hooks.on(hooks.TaskFailed, handler)         # → disposer

        @hooks.on(hooks.TaskFailed)
        def handler(ev): ...                         # registered; returns handler
    """
    if callback is None:

        def _decorator(fn: Callable[[E], None]) -> Callable[[E], None]:
            _subscribe_filtered(event_type, fn)
            return fn

        return _decorator
    return _subscribe_filtered(event_type, callback)


def _subscribe_filtered(
    event_type: type[E], callback: Callable[[E], None]
) -> Callable[[], None]:
    def _filtered(ev: PymonikEvent) -> None:
        if isinstance(ev, event_type):
            callback(ev)  # type: ignore[arg-type]

    return subscribe(_filtered)


def active() -> bool:
    """True if any hook is registered. Call sites in hot paths guard the
    event-arg construction with this so the no-hooks path allocates
    nothing."""
    return bool(_subscribers)


def emit(event_cls: type[PymonikEvent], **fields: object) -> None:
    """Build and dispatch an event — but only if someone is listening.

    The fast path is a lock-free tuple read + empty check; no event is
    constructed and nothing is dispatched when there are no subscribers.
    """
    subs = _subscribers
    if not subs:
        return
    ev = event_cls(**fields)  # type: ignore[arg-type]
    for cb in subs:
        try:
            cb(ev)
        except Exception as e:  # noqa: BLE001 — a hook must never break core
            log.debug(
                "hook raised",
                hook=getattr(cb, "__qualname__", repr(cb)),
                event_type=type(ev).__name__,
                error=str(e),
            )


def _reset_for_tests() -> None:
    """Drop all subscribers. Test-only."""
    global _subscribers
    with _lock:
        _subscribers = ()


__all__ = [
    "PymonikEvent",
    "SessionOpened",
    "SessionClosed",
    "TaskSubmitted",
    "TaskCompleted",
    "TaskFailed",
    "TaskRetried",
    "Hook",
    "subscribe",
    "unsubscribe",
    "on",
    "active",
    "emit",
]
