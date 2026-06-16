"""End-to-end query-filter tests against a real ArmoniK cluster.

Opt-in: skipped unless ``AKCONFIG`` (or ``PYMONIK_ENDPOINT``) is set —
see ``conftest.cluster_client``. Run with:

    export AKCONFIG=/path/to/generated/armonik-cli.yaml
    uv run pytest -m e2e tests/test_query_filters_e2e.py

These exercise the *scalar* filter fields end-to-end (the maps fixed by
sourcing from the ArmoniK models) — confirming the cluster accepts each
field and returns the expected rows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pymonik import task
from tests.conftest import E2E_PARTITION

pytestmark = pytest.mark.e2e


@task
def _e2e_add(a: int, b: int) -> int:
    return a + b


@pytest.fixture(scope="module")
def workload(cluster_client):
    """Submit a small known batch once; reuse across the module."""
    with cluster_client.session(partition=E2E_PARTITION) as s:
        futs = _e2e_add.map(range(4), range(4))
        # One task carries a custom option, to exercise the options-map struct.
        tagged = _e2e_add.with_options(options={"e2etag": "yes"}).spawn(10, 10)
        vals = [f.result(timeout=120) for f in futs]
        tagged.result(timeout=120)
        yield SimpleNamespace(
            client=cluster_client,
            session=s,
            sid=s.session_id,
            task_ids=[f.task_id for f in futs],
            result_ids=[f.result_id for f in futs],
            tagged_task_id=tagged.task_id,
            values=vals,
        )


def test_results_scoped_by_session(workload):
    listed = {r.id for r in workload.session.results.list()}
    # All task outputs are present; the scope returns this session's results.
    assert set(workload.result_ids) <= listed
    assert workload.session.results.count() == len(listed)


def test_sessions_where_session_id(workload):
    # Was ValueError('unknown field') before the field map was completed.
    assert workload.client.sessions.where(session_id=workload.sid).count() == 1


def test_partitions_where_id(workload):
    assert workload.client.partitions.where(id=E2E_PARTITION).count() == 1


def test_results_where_owner_task_id(workload):
    tid = workload.task_ids[0]
    owned = workload.client.results.where(owner_task_id=tid).list()
    assert owned, "expected at least the task's output result"
    # The producing task's output is in this session.
    assert {r.id for r in owned} <= set(
        r.id for r in workload.session.results.list()
    )


def test_tasks_scoped_and_ordered(workload):
    tasks = workload.client.tasks.where(session_id=workload.sid).order_by("created_at").list()
    # 4 from the map + 1 tagged task.
    expected_ids = set(workload.task_ids) | {workload.tagged_task_id}
    assert {t.id for t in tasks} == expected_ids
    assert all(t.session_id == workload.sid for t in tasks)


def test_results_status_filter(workload):
    from armonik.common import ResultStatus

    done = workload.session.results.where(status=ResultStatus.COMPLETED).count()
    assert done >= len(workload.result_ids)


def test_results_name_startswith(workload):
    # String predicate suffix over a scalar field, server-side.
    out_ids = set(workload.result_ids)
    named = workload.session.results.where(name__startswith="").list()
    assert out_ids <= {r.id for r in named}


# ---------- array + struct filters ----------

def test_session_partition_ids_contains(workload):
    # The session was created on E2E_PARTITION, so its partition_ids contains it.
    n = workload.client.sessions.where(
        session_id=workload.sid, partition_ids__contains=E2E_PARTITION
    ).count()
    assert n == 1


def test_task_options_map_key(workload):
    c = workload.client
    # The tagged task carries options["e2etag"] == "yes".
    assert c.tasks.where(session_id=workload.sid, options__e2etag="yes").count() == 1
    assert c.tasks.where(session_id=workload.sid, options__e2etag="no").count() == 0


def test_task_output_error_struct(workload):
    # No task failed, so none has an error message containing this token.
    n = workload.client.tasks.where(
        session_id=workload.sid, output__error__contains="zzz_does_not_exist"
    ).count()
    assert n == 0
