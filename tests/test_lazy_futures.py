"""Lazy future materialization.

The completion loop marks a future COMPLETED without downloading its
bytes; the bytes are fetched on the first ``.result()`` / ``await``.
A pipeline the client never reads must therefore not materialize its
intermediate results.
"""

from __future__ import annotations

import pytest

from pymonik import TaskFailed, task
from pymonik.testing import LocalCluster


@task
def inc(x: int) -> int:
    return x + 1


@task
def total(xs: list[int]) -> int:
    return sum(xs)


def test_intermediates_completed_but_not_materialized():
    with LocalCluster() as c, c.session():
        parts = inc.map(range(5))          # 5 intermediate futures
        terminal = total.spawn(parts)      # depends on all 5
        assert terminal.result(timeout=10) == sum(range(1, 6))  # 1+2+3+4+5

        # Every intermediate is DONE (status known)...
        assert all(f.done for f in parts)
        # ...but NONE was materialized — the client never read them; the
        # worker consumed them via data_dependencies.
        assert all(not f._materialized for f in parts)
        # The terminal, which we read, IS materialized.
        assert terminal._materialized


def test_reading_an_intermediate_materializes_only_it():
    with LocalCluster() as c, c.session():
        parts = inc.map(range(5))
        total.spawn(parts).result(timeout=10)

        parts[2].result(timeout=10)        # read exactly one intermediate
        assert parts[2]._materialized
        assert sum(1 for f in parts if f._materialized) == 1


def test_materialize_is_counted_once_per_future(monkeypatch):
    from pymonik.testing.local import LocalSession

    calls: list[str] = []
    orig = LocalSession._materialize_result

    def counting(self, result_id):
        calls.append(result_id)
        return orig(self, result_id)

    monkeypatch.setattr(LocalSession, "_materialize_result", counting)

    with LocalCluster() as c, c.session():
        parts = inc.map(range(4))
        terminal = total.spawn(parts)
        # Read the terminal twice — must download once.
        assert terminal.result(timeout=10) == sum(range(1, 5))
        assert terminal.result(timeout=10) == sum(range(1, 5))

    # Exactly one materialize call total: the terminal, once. The four
    # intermediates were never downloaded.
    assert calls == [terminal.result_id]


def test_results_list_materializes_all():
    with LocalCluster() as c, c.session():
        parts = inc.map(range(5))
        vals = parts.results(timeout=10)
        assert vals == [1, 2, 3, 4, 5]
        assert all(f._materialized for f in parts)


def test_wait_does_not_materialize():
    with LocalCluster() as c, c.session():
        f = inc.spawn(41)
        f.wait(timeout=10)
        assert f.done and not f._materialized
        assert f.result(timeout=10) == 42
        assert f._materialized


def test_failure_still_propagates_lazily():
    @task
    def boom(x: int) -> int:
        raise ValueError("nope")

    with LocalCluster() as c, c.session():
        f = boom.spawn(1)
        with pytest.raises(TaskFailed):
            f.result(timeout=10)


@pytest.mark.anyio
async def test_await_materializes_lazily():
    async with LocalCluster() as c, c.session_async():
        parts = inc.map(range(3))
        terminal = total.spawn(parts)
        assert await terminal == sum(range(1, 4))
        assert terminal._materialized
        assert all(not f._materialized for f in parts)


@pytest.fixture
def anyio_backend():
    return "asyncio"
