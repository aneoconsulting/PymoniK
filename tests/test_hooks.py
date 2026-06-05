"""Client-side lifecycle hooks.

Core feature — no optional extra. Covers the public API (subscribe /
on / unsubscribe), the end-to-end emission through LocalCluster, and
the two load-bearing contracts: near-zero cost when unused (no event
constructed) and isolation (a raising hook never breaks core).
"""

from __future__ import annotations

import pytest

from pymonik import hooks, task
from pymonik.testing import LocalCluster


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Each test starts and ends with an empty registry."""
    hooks._reset_for_tests()
    yield
    hooks._reset_for_tests()


# ---------- API ----------


def test_subscribe_receives_all_events_and_unsubscribe():
    seen = []
    unsub = hooks.subscribe(seen.append)
    hooks.emit(hooks.SessionOpened, session_id="s")
    hooks.emit(hooks.TaskCompleted, session_id="s", task_id="t", result_id="r")
    assert [type(e).__name__ for e in seen] == ["SessionOpened", "TaskCompleted"]

    unsub()
    hooks.emit(hooks.SessionClosed, session_id="s")
    assert len(seen) == 2  # nothing after unsubscribe


def test_on_filters_by_type_call_and_decorator_forms():
    fails, alls = [], []
    hooks.subscribe(alls.append)
    hooks.on(hooks.TaskFailed, fails.append)

    @hooks.on(hooks.SessionOpened)
    def _opened(ev):
        alls.append(("opened", ev.session_id))

    hooks.emit(hooks.TaskFailed, session_id="s", task_id="t", result_id="r",
               error_type="ValueError", message="boom")
    hooks.emit(hooks.SessionOpened, session_id="s", partitions=("p",))

    assert [type(e).__name__ for e in fails] == ["TaskFailed"]
    # the all-subscriber saw both; the decorator saw only SessionOpened
    assert ("opened", "s") in alls


def test_event_carries_monotonic_timestamp_for_elapsed():
    seen = []
    hooks.subscribe(seen.append)
    hooks.emit(hooks.TaskSubmitted, session_id="s", task_id="t", task_name="f")
    hooks.emit(hooks.TaskCompleted, session_id="s", task_id="t", result_id="r")
    submitted, completed = seen
    assert completed.at >= submitted.at  # consumers diff these for elapsed


# ---------- contract: near-zero when unused ----------


def test_emit_constructs_nothing_when_no_subscribers():
    from dataclasses import dataclass

    constructed = []

    @dataclass(slots=True, frozen=True, kw_only=True)
    class Sentinel(hooks.PymonikEvent):
        def __post_init__(self):
            constructed.append(1)

    # No subscribers → emit must not build the event.
    hooks.emit(Sentinel, session_id="s")
    assert constructed == []
    assert hooks.active() is False

    # With a subscriber, it does build (and deliver) it.
    hooks.subscribe(lambda ev: None)
    hooks.emit(Sentinel, session_id="s")
    assert constructed == [1]


# ---------- contract: a raising hook never breaks core ----------


def test_raising_hook_is_isolated():
    delivered = []

    def boom(ev):
        raise RuntimeError("hook bug")

    hooks.subscribe(boom)
    hooks.subscribe(delivered.append)  # registered after the bad one

    # emit must not raise, and the good hook still runs.
    hooks.emit(hooks.SessionClosed, session_id="s")
    assert len(delivered) == 1


def test_raising_hook_does_not_break_task_resolution():
    hooks.subscribe(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))

    @task
    def add(a: int, b: int) -> int:
        return a + b

    # A buggy hook must not fail the task.
    with LocalCluster() as c, c.session():
        assert add.spawn(2, 3).result(timeout=10) == 5


# ---------- end-to-end through LocalCluster ----------


def test_localcluster_emits_full_lifecycle():
    from collections import Counter

    seen = []
    hooks.subscribe(seen.append)

    @task
    def add(a: int, b: int) -> int:
        return a + b

    @task
    def sum_all(xs: list[int]) -> int:
        return sum(xs)

    with LocalCluster() as c, c.session():
        parts = add.map(range(3), range(1, 4))
        total = sum_all.spawn(parts)
        assert total.result(timeout=10) == (0 + 1) + (1 + 2) + (2 + 3)

    kinds = Counter(type(e).__name__ for e in seen)
    assert kinds["SessionOpened"] == 1
    assert kinds["SessionClosed"] == 1
    assert kinds["TaskSubmitted"] == 4  # 3 add + 1 sum_all
    assert kinds["TaskCompleted"] == 4


def test_created_by_links_worker_spawned_subtasks():
    """A task spawned from inside a @task body carries the parent's id."""
    submitted: dict[str, str | None] = {}

    @hooks.on(hooks.TaskSubmitted)
    def _record(ev):
        submitted[ev.task_id] = ev.created_by

    @task
    def leg(x: int) -> int:
        return x * 2

    @task
    def basket(xs: list[int]) -> list[int]:
        # Fan out from inside the worker body.
        return leg.map(xs).results()

    with LocalCluster() as c, c.session():
        basket.spawn([1, 2, 3]).result(timeout=10)

    # The top-level basket has no parent; the leg children point at it.
    parents = set(submitted.values())
    assert None in parents               # basket itself
    assert any(p is not None for p in parents)  # leg children created_by basket
