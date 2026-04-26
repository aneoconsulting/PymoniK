"""``.wait()`` — block without retrieving the value or raising on failure.

`.result()` blocks AND returns the value AND raises on failure;
`.wait()` only blocks. Useful when you want to synchronise without
surfacing the value (e.g. fan out, wait for everything, then decide
what to do based on `.done` / `.task_id`).
"""

from __future__ import annotations

import pytest

from pymonik import MultiResult, TaskFailed, TaskTimeout, task
from pymonik.testing import LocalCluster


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def boom(x: int) -> int:
    raise ValueError(f"nope {x}")


def test_future_wait_returns_self_for_chaining():
    with LocalCluster() as client:
        with client.session() as s:
            fut = add.spawn(2, 3)
            same = fut.wait()
            assert same is fut
            assert fut.done
            assert fut.result() == 5  # cheap retrieval after wait


def test_future_wait_doesnt_raise_on_failure():
    """A failed task is *done*; wait() doesn't propagate the error."""
    with LocalCluster() as client:
        with client.session() as s:
            fut = boom.spawn(7)
            fut.wait(timeout=10)             # no raise
            assert fut.done
            with pytest.raises(TaskFailed):  # raise lives on .result()
                fut.result()


def test_future_wait_timeout():
    with LocalCluster() as client:
        with client.session() as s:
            fut = add.spawn(2, 3)
            fut.wait(timeout=10)
    # After session exits and fut is resolved, wait still works (already done).


def test_future_wait_timeout_raises_when_unfinished():
    """A future that never resolves should raise TaskTimeout."""
    from pymonik.future import Future

    fut: Future[int] = Future.__new__(Future)
    import threading

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

    with pytest.raises(TaskTimeout):
        fut.wait(timeout=0.1)


def test_futurelist_wait_returns_self():
    with LocalCluster() as client:
        with client.session() as s:
            futs = add.map(range(4), range(1, 5))
            same = futs.wait()
            assert same is futs
            assert all(f.done for f in futs)
            assert futs.results() == [1, 3, 5, 7]


def test_multiresulthandle_wait_returns_self():
    @task
    def split(x: int):
        return MultiResult(double=x * 2, triple=x * 3)

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(5)
            same = out.wait()
            assert same is out
            assert out.done
            assert out.result() == {"double": 10, "triple": 15}


def test_wait_then_result_is_two_step_chain():
    """The composed pattern: wait without retrieving, then retrieve."""
    with LocalCluster() as client:
        with client.session() as s:
            fut = add.spawn(10, 20)
            value = fut.wait(timeout=10).result()
            assert value == 30


def test_future_wait_async_returns_self():
    """Async ``.wait_async()`` round-trip. asyncio-only — the rest of
    PymoniK's async surface is also asyncio-pinned today."""
    import asyncio

    async def _run():
        async with LocalCluster() as client:
            async with client.session_async() as s:
                fut = add.spawn(2, 3)
                same = await fut.wait_async()
                assert same is fut
                assert fut.done
                assert fut.result() == 5

    asyncio.run(_run())


def test_futurelist_wait_async_returns_self():
    import asyncio

    async def _run():
        async with LocalCluster() as client:
            async with client.session_async() as s:
                futs = add.map(range(3), range(1, 4))
                same = await futs.wait_async()
                assert same is futs
                assert all(f.done for f in futs)

    asyncio.run(_run())
