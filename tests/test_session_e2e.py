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
