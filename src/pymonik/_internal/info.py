"""Homogenised resource Info classes.

The upstream ``armonik.common`` types use different id field names per
resource (``Task.id`` vs ``Result.result_id`` vs ``Session.session_id``
vs ``Partition.id``). The fluent introspection layer wraps them in
matching Info classes that all expose ``.id`` so user code can iterate
over heterogeneous result lists without learning four name variants.

Each ``*Info`` is a frozen dataclass — pure data, no methods that talk
to a session. Mutations (cancel / delete / download / etc.) live on the
``Query`` (batched, single round-trip) rather than on the row, so the
common case ("delete every result older than X") is one RPC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from armonik.common import Partition as _ArmPartition
    from armonik.common import Result as _ArmResult
    from armonik.common import Session as _ArmSession
    from armonik.common import Task as _ArmTask


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskInfo:
    """Snapshot of a task. ``id`` is always populated; everything else is
    nullable since ArmoniK's list endpoints sometimes return summaries."""

    id: str
    session_id: str
    status: Any  # armonik.common.TaskStatus
    partition_id: Optional[str] = None
    priority: Optional[int] = None
    created_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    creation_to_end_duration: Optional[timedelta] = None
    expected_output_ids: list[str] = field(default_factory=list)
    data_dependencies: list[str] = field(default_factory=list)
    payload_id: Optional[str] = None
    pod_hostname: Optional[str] = None
    error: Optional[str] = None
    status_message: Optional[str] = None

    @classmethod
    def from_armonik(cls, t: "_ArmTask") -> "TaskInfo":
        # ``Task.error`` lives under ``output.error`` on the upstream model.
        err: Optional[str] = None
        out = getattr(t, "output", None)
        if out is not None:
            err = getattr(out, "error", None) or None
        return cls(
            id=t.id,
            session_id=t.session_id,
            status=t.status,
            partition_id=getattr(getattr(t, "options", None), "partition_id", None),
            priority=getattr(getattr(t, "options", None), "priority", None),
            created_at=t.created_at,
            submitted_at=t.submitted_at,
            started_at=t.started_at,
            ended_at=t.ended_at,
            creation_to_end_duration=t.creation_to_end_duration,
            expected_output_ids=list(t.expected_output_ids or []),
            data_dependencies=list(t.data_dependencies or []),
            payload_id=getattr(t, "payload_id", None),
            pod_hostname=getattr(t, "pod_hostname", None),
            error=err,
            status_message=getattr(t, "status_message", None),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultInfo:
    """Snapshot of a result. ``id`` (renamed from upstream ``result_id``)
    plus the rest. ``size_bytes`` is the storage-backend payload size when
    known."""

    id: str
    session_id: str
    name: Optional[str] = None
    status: Any  # armonik.common.ResultStatus
    size_bytes: Optional[int] = None
    owner_task_id: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_by: Optional[str] = None

    @classmethod
    def from_armonik(cls, r: "_ArmResult") -> "ResultInfo":
        return cls(
            id=r.result_id,
            session_id=r.session_id,
            name=getattr(r, "name", None),
            status=r.status,
            size_bytes=getattr(r, "size", None),
            owner_task_id=getattr(r, "owner_task_id", None),
            created_at=getattr(r, "created_at", None),
            completed_at=getattr(r, "completed_at", None),
            created_by=getattr(r, "created_by", None),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionInfo:
    """Snapshot of a session. ``id`` (renamed from upstream
    ``session_id``)."""

    id: str
    status: Any  # armonik.common.SessionStatus
    partition_ids: list[str] = field(default_factory=list)
    client_submission: Optional[bool] = None
    worker_submission: Optional[bool] = None
    created_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    purged_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    duration: Optional[timedelta] = None

    @classmethod
    def from_armonik(cls, s: "_ArmSession") -> "SessionInfo":
        return cls(
            id=s.session_id,
            status=s.status,
            partition_ids=list(s.partition_ids or []),
            client_submission=getattr(s, "client_submission", None),
            worker_submission=getattr(s, "worker_submission", None),
            created_at=getattr(s, "created_at", None),
            cancelled_at=getattr(s, "cancelled_at", None),
            closed_at=getattr(s, "closed_at", None),
            purged_at=getattr(s, "purged_at", None),
            deleted_at=getattr(s, "deleted_at", None),
            duration=getattr(s, "duration", None),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartitionInfo:
    """Snapshot of a partition."""

    id: str
    priority: Optional[int] = None
    pod_max: Optional[int] = None
    pod_reserved: Optional[int] = None
    preemption_percentage: Optional[int] = None
    parent_partition_ids: list[str] = field(default_factory=list)
    pod_configuration: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_armonik(cls, p: "_ArmPartition") -> "PartitionInfo":
        return cls(
            id=p.id,
            priority=getattr(p, "priority", None),
            pod_max=getattr(p, "pod_max", None),
            pod_reserved=getattr(p, "pod_reserved", None),
            preemption_percentage=getattr(p, "preemption_percentage", None),
            parent_partition_ids=list(getattr(p, "parent_partition_ids", []) or []),
            pod_configuration=dict(getattr(p, "pod_configuration", {}) or {}),
        )
