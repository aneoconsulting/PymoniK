"""High-level fan-in primitives over Futures.

Mirrors ``asyncio.gather`` / ``asyncio.as_completed`` semantics but operates
on PymoniK ``Future`` / ``FutureList``. Both async and sync forms are
provided; the async form is the canonical one and the sync form is a thin
wrapper for users not in an event loop.

Inputs are flexible: pass varargs of ``Future``, a single ``FutureList``,
a list/iterable of futures, or any mix. Nested ``FutureList`` containers
are flattened one level (matching their iter protocol).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Iterator

from pymonik.future import Future, FutureList

if TYPE_CHECKING:
    pass


# Brief poll interval for sync as_completed when no future has resolved yet.
# Trade-off: smaller = more responsive, more CPU. 50 ms is invisible in
# practice and matches typical task latencies.
_AS_COMPLETED_POLL_S = 0.05


def _flatten(items: Iterable[Any]) -> Iterator[Future[Any]]:
    """Flatten a mix of Futures, FutureLists, and iterables of either."""
    for item in items:
        if isinstance(item, Future):
            yield item
        elif isinstance(item, FutureList):
            yield from item
        elif hasattr(item, "__iter__") and not isinstance(item, (str, bytes)):
            for sub in item:
                if isinstance(sub, Future):
                    yield sub
                elif isinstance(sub, FutureList):
                    yield from sub
                else:
                    raise TypeError(
                        f"gather/as_completed: expected Future or FutureList, "
                        f"got {type(sub).__name__}"
                    )
        else:
            raise TypeError(
                f"gather/as_completed: expected Future / FutureList / iterable "
                f"of Futures, got {type(item).__name__}"
            )


# ---------- async ----------

async def gather(
    *futures: Any,
    return_exceptions: bool = False,
    timeout: float | None = None,
) -> list[Any]:
    """Wait for every future and return their results in submission order.

    Args:
        *futures: ``Future`` objects, a ``FutureList``, or a mix.
        return_exceptions: if ``True``, exceptions are returned in-line
            instead of raised — same semantics as ``asyncio.gather``.
        timeout: per-future deadline; ``None`` waits forever.

    Returns:
        A list of results (or exceptions when ``return_exceptions=True``).
    """
    flat = list(_flatten(futures))
    coros = [f._await(timeout) for f in flat]
    return await asyncio.gather(*coros, return_exceptions=return_exceptions)


async def as_completed(
    *futures: Any,
    timeout: float | None = None,
) -> AsyncIterator[Future[Any]]:
    """Yield futures one at a time, in completion order, as they resolve.

    The yielded value is the resolved ``Future`` itself — call ``await
    fut`` (or ``fut.result()``) to get its value or re-raise its error.
    Mirrors ``asyncio.as_completed`` but yields Futures rather than
    awaitables.
    """
    flat = list(_flatten(futures))
    if not flat:
        return

    # Wrap each Future in an asyncio.Task so we can wait on them as a set.
    pending: dict[asyncio.Task[Any], Future[Any]] = {
        asyncio.create_task(f._await(timeout)): f for f in flat
    }
    try:
        while pending:
            done, _ = await asyncio.wait(
                pending.keys(), return_when=asyncio.FIRST_COMPLETED
            )
            for d in done:
                fut = pending.pop(d)
                # Drain the task's exception state so asyncio doesn't warn.
                if d.exception() is not None:
                    pass  # the Future already carries the typed error
                yield fut
    finally:
        # Cancel any tasks we didn't get to (caller broke out early).
        for t in pending:
            t.cancel()


# ---------- sync ----------

def gather_sync(
    *futures: Any,
    return_exceptions: bool = False,
    timeout: float | None = None,
) -> list[Any]:
    """Sync sibling of :func:`gather`.

    Blocks the calling thread until every future resolves; returns results
    in submission order.
    """
    flat = list(_flatten(futures))
    out: list[Any] = []
    for f in flat:
        try:
            out.append(f.result(timeout=timeout))
        except Exception as e:
            if return_exceptions:
                out.append(e)
            else:
                raise
    return out


def as_completed_sync(
    *futures: Any,
    timeout: float | None = None,
) -> Iterator[Future[Any]]:
    """Sync sibling of :func:`as_completed`.

    Polls every ~50 ms; suitable for batches up to a few hundred futures.
    Above that, prefer the async form (``async for f in as_completed(...)``)
    which uses ``asyncio.wait`` and scales without per-iteration polling.

    Args:
        *futures: same shapes accepted as :func:`gather`.
        timeout: total wall-clock deadline across the iteration; raises
            :class:`TaskTimeout` on the first not-yet-done future when
            the deadline elapses.
    """
    flat = list(_flatten(futures))
    pending: list[Future[Any]] = list(flat)
    deadline = None if timeout is None else time.monotonic() + timeout

    while pending:
        # Look for any done future first.
        for i, f in enumerate(pending):
            if f.done:
                yield pending.pop(i)
                break
        else:
            # None done yet; either wait briefly or fail on deadline.
            if deadline is not None and time.monotonic() >= deadline:
                # Yield-then-raise from the next .result() call.
                pending[0].result(timeout=0.0)  # raises TaskTimeout
            # Block on the first pending future for up to the poll
            # interval; whoever completes first wakes us up.
            pending[0]._done.wait(timeout=_AS_COMPLETED_POLL_S)
