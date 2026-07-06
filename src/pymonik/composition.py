"""Fan-in over Futures: ``gather`` and ``as_completed``.

``gather(...)`` flattens any mix of futures and ``FutureList``s into a single
``FutureList`` — so it has exactly the same doors as ``Task.map``: ``.results()``
/ ``await`` for values, ``.outcomes()`` to settle without raising, plus
``.done`` / ``.cancel()``.

``as_completed(...)`` returns a single object that is both iterable and
async-iterable — pick ``for`` or ``async for`` to match your world; each
yielded item is a resolved ``Future``.

Inputs are flexible: pass varargs of ``Future``, a single ``FutureList``, a
list/iterable of either, or any mix. Nested ``FutureList`` containers flatten
one level (matching their iter protocol).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

from pymonik.errors import TaskTimeout
from pymonik.future import Future, FutureList, MultiResultHandle, _ensure_off_loop

# Brief poll interval for sync as_completed when no future has resolved yet.
# Trade-off: smaller = more responsive, more CPU. 50 ms is invisible in
# practice and matches typical task latencies.
_AS_COMPLETED_POLL_S = 0.05


def _flatten(items: Iterable[Any]) -> list[Future[Any]]:
    """Flatten a mix of Futures, handles, FutureLists, and iterables of either.

    Composition operates at per-field granularity: a ``MultiResultHandle``
    (bare, or inside a ``FutureList`` from a multi-output ``.map()``) fans
    out to its per-field Futures. The sync ``as_completed`` door blocks on
    one done-event per element, which only a ``Future`` has.
    """
    shallow: list[Any] = []
    for item in items:
        if isinstance(item, (Future, MultiResultHandle)):
            shallow.append(item)
        elif isinstance(item, FutureList):
            shallow.extend(item)
        elif hasattr(item, "__iter__") and not isinstance(item, (str, bytes)):
            for sub in item:
                if isinstance(sub, (Future, MultiResultHandle)):
                    shallow.append(sub)
                elif isinstance(sub, FutureList):
                    shallow.extend(sub)
                else:
                    raise TypeError(
                        f"gather/as_completed: expected Future, MultiResultHandle "
                        f"or FutureList, got {type(sub).__name__}"
                    )
        else:
            raise TypeError(
                f"gather/as_completed: expected Future / MultiResultHandle / "
                f"FutureList / iterable of those, got {type(item).__name__}"
            )
    out: list[Future[Any]] = []
    for f in shallow:
        if isinstance(f, MultiResultHandle):
            out.extend(f)  # __iter__ yields the per-field Futures
        else:
            out.append(f)
    return out


class AsCompleted:
    """Yields the futures of a batch as they resolve. Iterable *and* async-iterable.

    The yielded value is the resolved :class:`pymonik.Future` itself — call
    ``fut.result()`` (sync) or ``await fut`` (async) to get its value or
    re-raise its error::

        for fut in as_completed(batch):        # sync
            print(fut.result())

        async for fut in as_completed(batch):  # async
            print(await fut)
    """

    __slots__ = (
        "_futures",
        "_timeout",
    )

    def __init__(self, futures: list[Future[Any]], timeout: float | None = None) -> None:
        self._futures = futures
        self._timeout = timeout

    def _timeout_error(self, unresolved: int) -> TaskTimeout:
        return TaskTimeout(
            message=(
                f"as_completed timed out after {self._timeout}s with "
                f"{unresolved} of {len(self._futures)} futures unresolved"
            )
        )

    def __iter__(self) -> Iterator[Future[Any]]:
        _ensure_off_loop("for ... in as_completed(...)")
        pending = list(self._futures)
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while pending:
            for i, f in enumerate(pending):
                if f.done:
                    yield pending.pop(i)
                    break
            else:
                if deadline is None:
                    wait_for = _AS_COMPLETED_POLL_S
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._timeout_error(len(pending))
                    wait_for = min(_AS_COMPLETED_POLL_S, remaining)
                # None done yet; block on the first pending future for up to
                # the poll interval — whoever completes first wakes us.
                pending[0]._done.wait(timeout=wait_for)

    def __aiter__(self) -> AsyncIterator[Future[Any]]:
        return self._aiter_impl()

    async def _aiter_impl(self) -> AsyncIterator[Future[Any]]:
        if not self._futures:
            return
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        pending: dict[asyncio.Task[Any], Future[Any]] = {
            asyncio.create_task(f._await()): f for f in self._futures
        }
        try:
            while pending:
                # The deadline spans the whole iteration (matching the sync
                # door and concurrent.futures.as_completed), so each wait gets
                # the *remaining* time, not the full timeout. Clamped to 0 so
                # already-resolved futures still yield at the deadline.
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                done, _ = await asyncio.wait(
                    pending.keys(), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise self._timeout_error(len(pending))
                for d in done:
                    fut = pending.pop(d)
                    # Drain the task's exception so asyncio doesn't warn — the
                    # Future already carries the typed error for the caller.
                    if d.exception() is not None:
                        pass
                    yield fut
        finally:
            for t in pending:  # caller broke out early or timed out — cancel the rest
                t.cancel()


def gather(*futures: Any) -> FutureList[Any]:
    """Flatten any mix of futures / ``FutureList``s into one ``FutureList``.

    The returned ``FutureList`` is waited on exactly like one from
    ``Task.map``: ``await gather(...)`` (async) or ``gather(...).results()``
    (sync) for the values in order, ``gather(...).outcomes()`` to settle every
    member without raising, ``.done`` / ``.cancel()`` as usual.
    """
    return FutureList(_flatten(futures))


def as_completed(*futures: Any, timeout: float | None = None) -> AsCompleted:
    """Iterate a batch's futures in completion order (sync or async).

    Returns an object usable with both ``for`` and ``async for``; each yielded
    item is a resolved :class:`pymonik.Future`. ``timeout`` is the overall
    deadline in seconds for the whole iteration (like
    ``concurrent.futures.as_completed``); if it expires with futures still
    unresolved, :class:`pymonik.TaskTimeout` is raised.
    """
    return AsCompleted(_flatten(futures), timeout)
