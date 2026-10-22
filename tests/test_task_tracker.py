"""TaskTracker batch logic, fed with synthetic ArmoniK events (no cluster)."""

from __future__ import annotations

import pytest
from armonik.common import EventTypes, NewTaskEvent, TaskStatus, TaskStatusUpdateEvent

from pymonik import hooks
from pymonik._internal.info import TaskInfo
from pymonik._internal.tracker import TaskTracker

SID = "session-1"


@pytest.fixture
def seen():
    hooks._reset_for_tests()
    out: list = []
    hooks.subscribe(out.append)
    yield out
    hooks._reset_for_tests()


class _Tracker(TaskTracker):
    """No stream, no threads: lookups answer from ``infos``."""

    def __init__(self, infos: dict[str, TaskInfo]) -> None:
        self._sid = SID
        self._tasks = {}
        self.infos = infos
        self.lookups: list[list[str]] = []

    def _lookup(self, task_ids):
        ids = list(task_ids)
        self.lookups.append(ids)
        return [self.infos[i] for i in ids if i in self.infos]

    def _list_session(self):
        return list(self.infos.values())


def new(task_id: str, *parents: str, outs=("r",), deps=(), retry_of=()):
    return (
        EventTypes.NEW_TASK,
        NewTaskEvent(
            task_id=task_id,
            payload_id="p",
            origin_task_id=task_id,
            status=TaskStatus.CREATING,
            expected_output_keys=list(outs),
            data_dependencies=list(deps),
            retry_of_ids=list(retry_of),
            parent_task_ids=[SID, *parents],
        ),
    )


def status(task_id: str, s: TaskStatus):
    return (EventTypes.TASK_STATUS_UPDATE, TaskStatusUpdateEvent(task_id=task_id, status=s))


def info(task_id: str, s=TaskStatus.PROCESSING, *, node="pod-1", by=None, error=None, name="leaf"):
    return TaskInfo(
        id=task_id,
        session_id=SID,
        status=s,
        partition_id="cpu",
        pod_hostname=node,
        error=error,
        created_by=by,
        task_name=name,
        expected_output_ids=["r"],
    )


def kinds(seen):
    return [
        ("submitted", e.task_id, e.created_by)
        if isinstance(e, hooks.TaskSubmitted)
        else (e.status, e.task_id, e.node)
        for e in seen
    ]


def test_worker_subtask_is_announced_then_followed(seen):
    tr = _Tracker({"c": info("c", by="p")})
    tr._handle(
        [
            new("c", "p", deps=("x",)),
            status("c", TaskStatus.SUBMITTED),
            status("c", TaskStatus.PROCESSING),
        ]
    )
    tr._handle([status("c", TaskStatus.PROCESSING), status("c", TaskStatus.COMPLETED)])
    assert kinds(seen) == [
        ("submitted", "c", "p"),
        ("running", "c", "pod-1"),
        ("completed", "c", "pod-1"),
    ]
    sub = seen[0]
    assert (sub.task_name, sub.partition, sub.data_dependencies, sub.result_ids) == (
        "leaf",
        "cpu",
        ("x",),
        ("r",),
    )
    assert tr.lookups == [["c"]]  # one lookup; the node is known afterwards


def test_own_task_waits_for_registration(seen):
    tr = _Tracker({"t": info("t", node="pod-2")})
    tr._handle([new("t"), status("t", TaskStatus.PROCESSING)])
    assert seen == []  # TaskSubmitted comes from the client's own submission
    tr._handle([("own", "t"), status("t", TaskStatus.COMPLETED)])
    assert kinds(seen) == [("running", "t", "pod-2"), ("completed", "t", "pod-2")]


def test_failure_carries_the_worker_error(seen):
    tr = _Tracker({"t": info("t", TaskStatus.ERROR, error="ValueError: boom")})
    tr._handle([("own", "t"), status("t", TaskStatus.ERROR)])
    (ev,) = seen
    assert (ev.status, ev.message) == ("failed", "ValueError: boom")


def test_armonik_retry_is_announced_as_a_new_attempt(seen):
    tr = _Tracker({"t2": info("t2")})
    tr._handle([("own", "t"), status("t", TaskStatus.RETRIED), new("t2", retry_of=("t",))])
    assert [(type(e).__name__, e.task_id) for e in seen] == [
        ("TaskStatusChanged", "t"),
        ("TaskSubmitted", "t2"),
    ]
    assert seen[1].attempt == 2 and seen[1].created_by is None


def test_sync_reports_the_final_state(seen):
    tr = _Tracker(
        {"t": info("t", TaskStatus.COMPLETED), "c": info("c", TaskStatus.COMPLETED, by="t")}
    )
    tr._handle([("own", "t"), status("t", TaskStatus.PROCESSING)])
    tr._handle([("sync", None)])
    assert kinds(seen) == [
        ("running", "t", "pod-1"),
        ("completed", "t", "pod-1"),
        ("submitted", "c", "t"),
        ("completed", "c", "pod-1"),
    ]
