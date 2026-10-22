"""Session lifecycle against a real ArmoniK cluster (opt-in, see ``conftest.cluster_client``)."""

from __future__ import annotations

import time

import pytest

from pymonik import task
from tests.conftest import E2E_PARTITION

pytestmark = pytest.mark.e2e


@task
def _one() -> int:
    return 1


def test_session_exit_does_not_wait_on_the_events_stream(cluster_client):
    s = cluster_client.session(partition=E2E_PARTITION)
    s.__enter__()
    try:
        assert _one.spawn().result(timeout=120) == 1
    finally:
        t0 = time.monotonic()
        s.__exit__(None, None, None)
    assert time.monotonic() - t0 < 1.0
    assert s._runner is not None and not s._runner.is_alive()


@task(partition="alias")
def _child(x: int) -> int:
    return x + 1


@task(partition="alias")
def _parent(x: int) -> int:
    return _child.tail(x)


def test_partition_map_reaches_worker_submitted_tasks(cluster_client):
    from pymonik import PymonikClient

    with PymonikClient(
        endpoint=cluster_client.endpoint,
        credentials=cluster_client.credentials,
        partition_map={"alias": E2E_PARTITION},
    ) as c, c.session(partition="alias") as s:
        assert s.partitions == (E2E_PARTITION,)
        assert _parent.spawn(1).result(timeout=120) == 2
        assert {t.partition_id for t in s.tasks.list()} == {E2E_PARTITION}


@task
def _leaf(i: int) -> int:
    return i * 2


@task
def _sum(xs: list[int]) -> int:
    return sum(xs)


@task
def _fan(n: int) -> int:
    return _sum.tail(_leaf.map(range(n)))


@task
def _boom() -> int:
    raise ValueError("boom")


def test_track_tasks_reports_every_task_with_its_node(cluster_client):
    from pymonik import PymonikClient, hooks

    seen: list = []
    unsub = hooks.subscribe(seen.append)
    try:
        with PymonikClient(
            endpoint=cluster_client.endpoint,
            credentials=cluster_client.credentials,
            track_tasks=True,
        ) as c, c.session(partition=E2E_PARTITION) as s:
            fan = _fan.spawn(3)
            assert fan.result(timeout=120) == 6
            bad = _boom.spawn()
            with pytest.raises(Exception, match="boom"):
                bad.result(timeout=120)
            listed = {t.id: t for t in s.tasks.list()}
    finally:
        unsub()

    mine = [e for e in seen if getattr(e, "session_id", None) == s.session_id]
    submitted = {e.task_id: e for e in mine if isinstance(e, hooks.TaskSubmitted)}
    assert set(submitted) == set(listed)  # worker-submitted tasks included
    for tid, t in listed.items():
        assert submitted[tid].created_by == t.created_by
        assert submitted[tid].task_name == t.task_name
    events: dict[str, list] = {}
    for e in mine:
        if isinstance(e, (hooks.TaskSubmitted, hooks.TaskStatusChanged)):
            events.setdefault(e.task_id, []).append(e)
    for tid, evs in events.items():
        assert isinstance(evs[0], hooks.TaskSubmitted), tid  # always first
        last = evs[-1]
        assert isinstance(last, hooks.TaskStatusChanged) and last.node == listed[tid].pod_hostname
    assert events[bad.task_id][-1].status == "failed"
    assert "boom" in (events[bad.task_id][-1].message or "")
    assert {events[t][-1].status for t in listed if t != bad.task_id} == {"completed"}
