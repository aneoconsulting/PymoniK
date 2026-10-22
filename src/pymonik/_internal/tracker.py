"""Follow a session's tasks on the ArmoniK events stream.

A session opened with ``PymonikClient(track_tasks=True)`` while a hook is
registered runs a ``TaskTracker``. It emits ``TaskStatusChanged`` for
every task of the session, and ``TaskSubmitted`` for the tasks this
client did not submit: tasks that workers submit (sub-tasks, tail calls)
and ArmoniK retries.

One thread reads ``Events.GetEvents`` (``NEW_TASK``,
``TASK_STATUS_UPDATE``) into a queue. Another handles the queue in
batches. The events carry no task name, partition, pod hostname or error
message: each batch fetches the ones it needs with one task listing.

The session registers each task it submits right after its
``TaskSubmitted`` hook. Status events of a parentless task wait for that
registration, so ``TaskSubmitted`` always comes first. ``close()`` lists
the session's tasks once more and reports what changed since the last
event.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable
from typing import Any

import grpc
from armonik.common import EventTypes, TaskStatus

import pymonik.hooks as hooks
from pymonik._internal._logging import get_logger
from pymonik._internal.events import EventStream
from pymonik._internal.info import TaskInfo
from pymonik._internal.query import TaskQuery, _make_context

log = get_logger(__name__)

STATUS = {
    TaskStatus.PROCESSING: "running",
    TaskStatus.COMPLETED: "completed",
    TaskStatus.ERROR: "failed",
    TaskStatus.TIMEOUT: "failed",
    TaskStatus.CANCELLING: "cancelled",
    TaskStatus.CANCELLED: "cancelled",
    TaskStatus.RETRIED: "retried",
}

_LOOKUP_CHUNK = 100


class _Task:
    __slots__ = ("own", "announced", "status", "node", "message", "info", "new", "queued")

    def __init__(self) -> None:
        # True: submitted by this client. False: by a worker, or an ArmoniK
        # retry. None: not known yet.
        self.own: bool | None = None
        self.announced = False  # its TaskSubmitted went out
        self.status: str | None = None  # last status emitted
        self.node: str | None = None
        self.message: str | None = None
        self.info: TaskInfo | None = None
        self.new: Any = None  # the NEW_TASK event
        self.queued: list[str] = []  # statuses not emitted yet


class TaskTracker:
    def __init__(self, session_id: str, channel: Any) -> None:
        self._sid = session_id
        self._channel = channel
        self._stream = EventStream(
            channel, session_id, [EventTypes.NEW_TASK, EventTypes.TASK_STATUS_UPDATE]
        )
        self._queue: queue.SimpleQueue[tuple[Any, Any]] = queue.SimpleQueue()
        self._tasks: dict[str, _Task] = {}
        self._closed = threading.Event()
        self._reader = threading.Thread(
            target=self._read, name=f"pymonik-track-{session_id[:8]}", daemon=True
        )
        self._worker = threading.Thread(
            target=self._work, name=f"pymonik-track-{session_id[:8]}-w", daemon=True
        )

    def start(self) -> None:
        self._reader.start()
        self._worker.start()

    def register(self, task_id: str) -> None:
        """A task this client submitted, after its ``TaskSubmitted`` hook."""
        self._queue.put(("own", task_id))

    def close(self, timeout: float = 5.0) -> None:
        """Stop the stream, report the final state of every task, stop."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._stream.cancel()
        done = threading.Event()
        self._queue.put(("sync", done))
        done.wait(timeout)
        self._queue.put(("stop", None))

    # ---- threads ----

    def _read(self) -> None:
        try:
            for kind, event in self._stream:
                self._queue.put((kind, event))
        except grpc.RpcError as e:
            if not self._closed.is_set():
                log.warning("task events stream ended", error=str(e))
        except Exception as e:  # noqa: BLE001 — background thread
            log.error("task events stream failed", error=str(e))

    def _work(self) -> None:
        while True:
            batch = [self._queue.get()]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._handle(batch)
            except Exception as e:  # noqa: BLE001 — tracking must not stop on one bad batch
                log.warning("task tracking failed", error=str(e))
            for kind, item in batch:
                if kind == "sync":
                    item.set()
            if any(kind == "stop" for kind, _ in batch):
                return

    # ---- batch handling (worker thread only) ----

    def _handle(self, batch: list[tuple[Any, Any]]) -> None:
        touched: dict[str, _Task] = {}

        def get(task_id: str) -> _Task:
            t = touched.get(task_id) or self._tasks.setdefault(task_id, _Task())
            touched[task_id] = t
            return t

        sync = False
        for kind, item in batch:
            if kind == EventTypes.NEW_TASK:
                t = get(item.task_id)
                t.new = item
                if t.own is None and (self._parent(item) or item.retry_of_ids):
                    t.own = False
            elif kind == EventTypes.TASK_STATUS_UPDATE:
                status = STATUS.get(item.status)
                if status is not None:
                    get(item.task_id).queued.append(status)
            elif kind == "own":
                t = get(item)
                t.own = True
                t.announced = True
            elif kind == "sync":
                sync = True

        if sync:
            for info in self._list_session():
                t = get(info.id)
                self._learn(t, info)
                status = STATUS.get(info.status)
                if status is not None and status != (t.queued[-1] if t.queued else t.status):
                    t.queued.append(status)
        else:
            need = [tid for tid, t in touched.items() if self._needs_lookup(t)]
            for info in self._lookup(need) if need else ():
                self._learn(get(info.id), info)

        for task_id, t in touched.items():
            self._flush(task_id, t)

    def _needs_lookup(self, t: _Task) -> bool:
        if not t.announced and t.own is False and t.info is None:
            return True  # name, partition, parent
        if t.queued and t.node is None:
            return True  # where it runs
        return "failed" in t.queued and t.message is None

    def _learn(self, t: _Task, info: TaskInfo) -> None:
        t.info = info
        t.node = info.pod_hostname or t.node
        t.message = info.error or t.message
        if t.own is None and info.created_by:
            t.own = False

    def _flush(self, task_id: str, t: _Task) -> None:
        if not t.announced:
            if t.own is not False:
                return  # this client's task: wait for register()
            self._announce(task_id, t)
            t.announced = True
        for status in t.queued:
            if status == t.status:
                continue
            t.status = status
            hooks.emit(
                hooks.TaskStatusChanged,
                session_id=self._sid,
                task_id=task_id,
                status=status,
                node=t.node,
                message=t.message if status == "failed" else None,
            )
        t.queued.clear()

    def _announce(self, task_id: str, t: _Task) -> None:
        new, info = t.new, t.info
        if new is not None:
            outs, deps = new.expected_output_keys, new.data_dependencies
            attempt, parent = 1 + len(new.retry_of_ids), self._parent(new)
        elif info is not None:
            outs, deps, attempt, parent = info.expected_output_ids, info.data_dependencies, 1, None
        else:
            return
        hooks.emit(
            hooks.TaskSubmitted,
            session_id=self._sid,
            task_id=task_id,
            task_name=(info.task_name if info else None) or "",
            result_ids=tuple(outs),
            data_dependencies=tuple(deps),
            partition=info.partition_id if info else None,
            attempt=attempt,
            created_by=(info.created_by if info else None) or parent,
        )

    def _parent(self, new: Any) -> str | None:
        """The task that submitted this one (the session id heads the list)."""
        parents = [p for p in new.parent_task_ids if p != self._sid]
        return parents[-1] if parents else None

    # ---- lookups ----

    def _lookup(self, task_ids: Iterable[str]) -> list[TaskInfo]:
        ids = list(task_ids)
        out: list[TaskInfo] = []
        try:
            for i in range(0, len(ids), _LOOKUP_CHUNK):
                chunk = ids[i : i + _LOOKUP_CHUNK]
                query = TaskQuery(_make_context(self._channel))
                out.extend(query.where(id__in=chunk).page_size(len(chunk)).list())
        except Exception as e:  # noqa: BLE001 — report without details
            log.debug("task lookup failed", error=str(e))
        return out

    def _list_session(self) -> list[TaskInfo]:
        try:
            ctx = _make_context(self._channel, scoped_session_id=self._sid)
            return TaskQuery(ctx).page_size(1000).list()
        except Exception as e:  # noqa: BLE001
            log.debug("task listing failed", error=str(e))
            return []
