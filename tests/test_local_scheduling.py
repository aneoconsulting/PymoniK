"""LocalCluster scheduling: inputs first, failures propagate, per-partition worker limits."""

from __future__ import annotations

import time

import pytest

from pymonik import task
from pymonik.errors import TaskFailed
from pymonik.future import Future
from pymonik.testing import LocalCluster


@task
def leaf(n: int) -> int:
    return n


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def tree(lo: int, hi: int) -> int:
    """Sum lo..hi-1: each node spawns two halves and tail-calls the collector."""
    if hi - lo <= 1:
        return lo
    mid = (lo + hi) // 2
    return add.tail(tree.spawn(lo, mid), tree.spawn(mid, hi))


def test_dynamic_tree_runs_on_two_threads():
    # Collectors queue behind their own children. They must not hold threads while waiting.
    with LocalCluster(max_workers=2) as c, c.session():
        assert tree.spawn(0, 32).result(timeout=30) == sum(range(32))


@task
def boom() -> int:
    raise ValueError("boom")


def test_a_failed_task_fails_its_dependents():
    with LocalCluster(max_workers=2) as c, c.session():
        bad = boom.spawn()
        downstream = add.spawn(add.spawn(bad, 1), 2)
        with pytest.raises(TaskFailed, match="unavailable"):
            downstream.result(timeout=10)
        with pytest.raises(TaskFailed, match="boom"):
            bad.result(timeout=10)


def test_an_unknown_input_fails_the_task():
    with LocalCluster(max_workers=2) as c, c.session() as s:
        ghost = Future(s, task_id="t-ghost", result_id="r-ghost")
        with pytest.raises(TaskFailed, match="unavailable"):
            add.spawn(ghost, 1).result(timeout=10)


@task
def span(i: int) -> tuple[float, float]:
    t = time.monotonic()
    time.sleep(0.05)
    return (t, time.monotonic())


def _overlap(futs) -> int:
    w = sorted(f.result(timeout=10) for f in futs)
    return max(sum(1 for a, b in w if a <= t < b) for t, _ in w)


def test_partition_workers_cap_concurrency_per_partition():
    cluster = LocalCluster(max_workers=8, partition_workers={"gpu": 1})
    with cluster as c, c.session(partition=["cpu", "gpu"]):
        on_gpu = [span.with_options(partition="gpu").spawn(i) for i in range(4)]
        on_cpu = [span.spawn(i) for i in range(4)]
        assert _overlap(on_gpu) == 1
        assert _overlap(on_cpu) > 1
