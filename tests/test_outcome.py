"""The wait surface: ``.result()`` / ``await`` (value) and ``.outcome()`` /
``.outcomes()`` (settle without raising), plus the event-loop guard.

`.result()` blocks and returns the value, raising on failure. `.outcome()`
blocks and returns an :class:`Outcome` that never raises on task failure —
you branch on ``.ok`` and read ``.error`` or ``.value`` (materialised
lazily). In async code the value door is ``await fut`` / ``await fl`` /
``await gather(...)``; the blocking doors guard against being called on a
running event loop.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from pymonik import (
    MultiResult,
    Outcome,
    TaskFailed,
    TaskTimeout,
    as_completed,
    gather,
    task,
)
from pymonik.errors import PymonikError
from pymonik.testing import LocalCluster


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def boom(x: int) -> int:
    raise ValueError(f"nope {x}")


# ---------------------------------------------------------------- value door


def test_result_returns_value():
    with LocalCluster() as client, client.session() as s:
        assert add.spawn(2, 3).result() == 5


def test_result_raises_on_failure():
    with LocalCluster() as client, client.session() as s:
        with pytest.raises(TaskFailed):
            boom.spawn(7).result()


# --------------------------------------------------------------- outcome door


def test_outcome_ok_carries_value_lazily():
    with LocalCluster() as client, client.session() as s:
        oc = add.spawn(2, 3).outcome()
        assert isinstance(oc, Outcome)
        assert oc.ok is True
        assert oc.error is None
        assert oc.value == 5
        assert oc.unwrap() == 5


def test_outcome_failed_never_raises():
    """A failed task is *settled*; outcome() reports it instead of raising."""
    with LocalCluster() as client, client.session() as s:
        oc = boom.spawn(7).outcome(timeout=10)
        assert oc.ok is False
        assert isinstance(oc.error, TaskFailed)
        # The error only surfaces if you ask for the value.
        with pytest.raises(TaskFailed):
            _ = oc.value


def test_outcome_timeout_raises_when_unfinished():
    """A future that never resolves raises TaskTimeout from outcome()/result()."""
    from pymonik.future import Future

    fut: Future[int] = Future.__new__(Future)
    fut._session = None  # type: ignore[assignment]
    fut._task_id = "test"
    fut._result_id = "test"
    fut._done = threading.Event()
    fut._aio_done = None
    fut._aio_loop = None
    fut._outcome = None
    fut._error = None
    fut._is_worker_stub = False
    fut._retry_state = None
    fut._retry_attempt = 0
    fut._cache_key = None
    fut._materialized = False
    fut._materialize_lock = threading.Lock()

    with pytest.raises(TaskTimeout):
        fut.outcome(timeout=0.1)
    with pytest.raises(TaskTimeout):
        fut.result(timeout=0.1)


def test_done_is_a_nonblocking_poll():
    with LocalCluster() as client, client.session() as s:
        fut = add.spawn(2, 3)
        fut.outcome()  # settle
        assert fut.done is True


# ----------------------------------------------------------------- FutureList


def test_futurelist_results_and_outcomes():
    with LocalCluster() as client, client.session() as s:
        fl = add.map(range(4), range(1, 5))
        assert fl.results() == [1, 3, 5, 7]
        assert fl.done is True

        ocs = add.map(range(3), range(1, 4)).outcomes()
        assert all(o.ok for o in ocs)
        assert [o.value for o in ocs] == [1, 3, 5]


def test_outcomes_mixed_success_and_failure():
    with LocalCluster() as client, client.session() as s:
        ok = add.spawn(1, 1)
        bad = boom.spawn(0)
        ocs = [ok.outcome(), bad.outcome()]
        assert [o.ok for o in ocs] == [True, False]
        assert ocs[0].value == 2
        assert isinstance(ocs[1].error, TaskFailed)


# --------------------------------------------------------------------- gather


def test_gather_results_sync():
    # gather returns a FutureList, so its sync values door is .results().
    with LocalCluster() as client, client.session() as s:
        assert gather(add.spawn(1, 1), add.spawn(2, 2)).results() == [2, 4]


def test_gather_outcomes_sync():
    # Settle a gathered batch without raising via the FutureList.outcomes() door.
    with LocalCluster() as client, client.session() as s:
        out = gather(add.spawn(1, 1), boom.spawn(0)).outcomes()
        assert isinstance(out[0], Outcome) and out[0].ok and out[0].value == 2
        assert isinstance(out[1], Outcome) and not out[1].ok


def test_gather_flattens_futurelists():
    with LocalCluster() as client, client.session() as s:
        fl = add.map(range(3), range(3))
        assert gather(add.spawn(10, 0), fl).results() == [10, 0, 2, 4]


# --------------------------------------------------------------- as_completed


def test_as_completed_sync():
    with LocalCluster() as client, client.session() as s:
        fl = add.map(range(5), range(5))
        assert sorted(f.result() for f in as_completed(fl)) == [0, 2, 4, 6, 8]


# --------------------------------------------------------- MultiResultHandle


def test_multiresulthandle_outcome():
    @task
    def split(x: int):
        return MultiResult(double=x * 2, triple=x * 3)

    with LocalCluster() as client, client.session() as s:
        out = split.spawn(5)
        oc = out.outcome()
        assert oc.ok is True
        assert out.done is True
        assert dict(oc.value) == {"double": 10, "triple": 15}


# ------------------------------------------------------------------ async door


def test_await_future_and_futurelist():
    async def _run():
        async with LocalCluster() as client:
            async with client.session_async() as s:
                assert await add.spawn(2, 3) == 5
                assert await add.map(range(4), range(1, 5)) == [1, 3, 5, 7]

    asyncio.run(_run())


def test_await_gather_values_and_raises():
    async def _run():
        async with LocalCluster() as client:
            async with client.session_async() as s:
                # await gather(...) → values, in submission order
                assert await gather(add.spawn(1, 1), add.spawn(2, 2)) == [2, 4]
                # a failing member surfaces the error (gather returns a
                # FutureList; await raises — settle-without-raising is the
                # sync .outcomes() door)
                with pytest.raises(TaskFailed):
                    await gather(add.spawn(3, 3), boom.spawn(0))

    asyncio.run(_run())


def test_async_for_as_completed():
    async def _run():
        got = []
        async with LocalCluster() as client:
            async with client.session_async() as s:
                async for f in as_completed(add.map(range(4), range(4))):
                    got.append(await f)
        assert sorted(got) == [0, 2, 4, 6]

    asyncio.run(_run())


def test_blocking_door_raises_on_event_loop():
    """The sync .result()/.outcome()/.results() doors raise a clear
    PymonikError when called from inside a running event loop."""

    async def _run():
        async with LocalCluster() as client:
            async with client.session_async() as s:
                fut = add.spawn(1, 1)
                with pytest.raises(PymonikError, match="event loop"):
                    fut.result()
                with pytest.raises(PymonikError, match="event loop"):
                    fut.outcome()
                with pytest.raises(PymonikError, match="event loop"):
                    add.map(range(2), range(2)).results()

    asyncio.run(_run())
