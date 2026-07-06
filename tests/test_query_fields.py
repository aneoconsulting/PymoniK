"""Each resource query exposes the filterable fields its ArmoniK *model*
supports — not just the handful the thin convenience ``*FieldFilter``
classes re-export.

Regression guard: session/partition queries used to expose only ``status``
/ ``priority`` respectively, so you couldn't even look one up by id. The
field maps are now sourced from the models (``Session`` / ``Partition`` /
``Result`` / ``Task``).
"""

from __future__ import annotations

import pytest

from pymonik._internal import query as q


def _ctx():
    return q._QueryContext(
        tasks=None, sessions=None, results=None, partitions=None,
        scoped_session_id=None,
    )


@pytest.mark.parametrize(
    "fields, expected",
    [
        (
            q._SESSION_FIELDS,
            {"id", "session_id", "status", "created_at",
             "client_submission", "worker_submission", "duration"},
        ),
        (
            q._PARTITION_FIELDS,
            {"id", "priority", "pod_max", "pod_reserved", "preemption_percentage"},
        ),
        (
            q._RESULT_FIELDS,
            {"id", "result_id", "session_id", "status", "name",
             "owner_task_id", "created_at", "completed_at"},
        ),
        (
            q._TASK_FIELDS,
            {"id", "task_id", "session_id", "status", "payload_id",
             "created_by", "processed_at"},
        ),
    ],
)
def test_resource_exposes_expected_fields(fields, expected):
    assert expected <= set(fields)


def test_headline_lookups_build_without_unknown_field():
    # Each of these raised ValueError("unknown field ...") before the maps
    # were completed from the models.
    q.SessionQuery(_ctx()).where(session_id="s")
    q.PartitionQuery(_ctx()).where(id="cpu")
    q.ResultQuery(_ctx()).where(owner_task_id="t")
    q.TaskQuery(_ctx()).where(payload_id="p")
