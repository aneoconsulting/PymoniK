"""Fluent introspection layer.

Three resource queries — ``TaskQuery``, ``SessionQuery``,
``ResultQuery``, ``PartitionQuery`` — share a small chainable surface:

    .where(**kwargs)        → AND-combined predicates
    .where_expr(filter)     → raw upstream Filter expression
    .order_by(*fields)      → ascending; '-field' for descending
    .limit(n) / .offset(n)
    .list() / .first() / .count()      (sync terminals)
    .list_async() / .first_async() / .count_async()
    for x in q: / async for x in q:    (paginated iteration)

Plus per-resource mutation verbs: cancel/delete/download for
tasks/results, full session lifecycle (cancel/pause/resume/close/
purge/delete/stop_submission) for sessions.

Predicate suffixes (Django-style):

    field=v             →  ==
    field__ne=v         →  !=
    field__lt=v         →  <      (ordered fields)
    field__lte / __gt / __gte
    field__in=[a, b, c] →  OR-chain of ==
    field__startswith=s →  string prefix
    field__endswith=s   →  string suffix
    field__contains=s   →  substring
    field__notcontains=s

Predicate names accept both the homogenised ``id`` and the upstream
``task_id`` / ``result_id`` / ``session_id`` so callers can use either
shape — that's also what makes ``session.tasks.where(id=tid)`` and
``client.tasks.where(task_id=tid)`` mean the same thing.

Mutations always materialise the matching set first via paginated
list calls. Bulk-friendly verbs (cancel_tasks, delete_result_data) are
batched; per-session verbs iterate one at a time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Generic,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    TypeVar,
)

import anyio
from pymonik._internal._logging import get_logger
from armonik.client import (
    ArmoniKPartitions,
    ArmoniKResults,
    ArmoniKSessions,
    ArmoniKTasks,
    PartitionFieldFilter,
    ResultFieldFilter,
    SessionFieldFilter,
    TaskFieldFilter,
)
from armonik.common import Direction
from armonik.common.filter import Filter

from pymonik._internal.info import (
    PartitionInfo,
    ResultInfo,
    SessionInfo,
    TaskInfo,
)

log = get_logger(__name__)


T = TypeVar("T")


# Default page size when iterating; configurable via PymonikClient if needed.
_DEFAULT_PAGE_SIZE = 100

# Cap how many items .list() will materialise before forcing pagination
# semantics on the caller. Above this users should iterate.
_MAX_LIST_ITEMS = 10_000


# Per-instance memoisation for ResultQuery's session-scoped candidate
# id enumeration. Keyed by id(query) to avoid touching the frozen state.
_SCOPED_CANDIDATE_CACHE: dict[int, list[str]] = {}


# ---------- predicate translation ----------

# Map "task_id" / "id" / "session_id" / etc. → upstream FieldFilter constant.
# ``id`` is the homogenised name; the resource-specific names also resolve.

_TASK_FIELDS: dict[str, Filter] = {
    "id": TaskFieldFilter.TASK_ID,
    "task_id": TaskFieldFilter.TASK_ID,
    "session_id": TaskFieldFilter.SESSION_ID,
    "status": TaskFieldFilter.STATUS,
    "priority": TaskFieldFilter.PRIORITY,
    "partition_id": TaskFieldFilter.PARTITION_ID,
    "max_retries": TaskFieldFilter.MAX_RETRIES,
    "max_duration": TaskFieldFilter.MAX_DURATION,
    "created_at": TaskFieldFilter.CREATED_AT,
    "submitted_at": TaskFieldFilter.SUBMITTED_AT,
    "received_at": TaskFieldFilter.RECEIVED_AT,
    "acquired_at": TaskFieldFilter.ACQUIRED_AT,
    "started_at": TaskFieldFilter.STARTED_AT,
    "ended_at": TaskFieldFilter.ENDED_AT,
    "pod_hostname": TaskFieldFilter.POD_HOSTNAME,
    "owner_pod_id": TaskFieldFilter.OWNER_POD_ID,
    "initial_task_id": TaskFieldFilter.INITIAL_TASK_ID,
    "engine_type": TaskFieldFilter.ENGINE_TYPE,
    "error": TaskFieldFilter.ERROR,
    "application_name": TaskFieldFilter.APPLICATION_NAME,
    "application_version": TaskFieldFilter.APPLICATION_VERSION,
    "application_namespace": TaskFieldFilter.APPLICATION_NAMESPACE,
    "application_service": TaskFieldFilter.APPLICATION_SERVICE,
    "creation_to_end_duration": TaskFieldFilter.CREATION_TO_END_DURATION,
    "processing_to_end_duration": TaskFieldFilter.PROCESSING_TO_END_DURATION,
    "pod_ttl": TaskFieldFilter.POD_TTL,
}

_RESULT_FIELDS: dict[str, Filter] = {
    "id": ResultFieldFilter.RESULT_ID,
    "result_id": ResultFieldFilter.RESULT_ID,
    "status": ResultFieldFilter.STATUS,
}

_SESSION_FIELDS: dict[str, Filter] = {
    "status": SessionFieldFilter.STATUS,
}

_PARTITION_FIELDS: dict[str, Filter] = {
    "priority": PartitionFieldFilter.PRIORITY,
}


def _build_predicate(
    fields: Mapping[str, Filter],
    name: str,
    op: Optional[str],
    value: Any,
) -> Filter:
    """Translate ``name__op=value`` into an upstream filter expression."""
    if name not in fields:
        allowed = ", ".join(sorted(fields))
        raise ValueError(
            f"unknown field {name!r} for this resource; "
            f"upstream supports: {allowed}"
        )
    f = fields[name]

    # datetime convenience: pass-through to the filter (upstream DateFilter
    # handles datetime values directly).
    if op is None:
        return f == value
    if op == "ne":
        return f != value
    if op == "lt":
        return f < value
    if op == "lte" or op == "le":
        return f <= value
    if op == "gt":
        return f > value
    if op == "gte" or op == "ge":
        return f >= value
    if op == "in":
        try:
            it = list(value)
        except TypeError as e:
            raise ValueError(f"{name}__in= expected an iterable, got {type(value).__name__}") from e
        if not it:
            raise ValueError(f"{name}__in= requires at least one value")
        first, *rest = it
        out = f == first
        for v in rest:
            out = out | (f == v)
        return out
    # String-only ops — these live as methods on the upstream filter.
    if op == "startswith":
        if not hasattr(f, "startswith"):
            raise ValueError(f"{name} doesn't support startswith")
        return f.startswith(value)
    if op == "endswith":
        if not hasattr(f, "endswith"):
            raise ValueError(f"{name} doesn't support endswith")
        return f.endswith(value)
    if op == "contains":
        if not hasattr(f, "contains_"):
            raise ValueError(f"{name} doesn't support contains")
        return f.contains_(value)
    if op == "notcontains":
        if not hasattr(f, "notcontains_"):
            raise ValueError(f"{name} doesn't support notcontains")
        return f.notcontains_(value)
    raise ValueError(f"unknown predicate suffix __{op}=")


def _filters_from_kwargs(
    fields: Mapping[str, Filter],
    kwargs: Mapping[str, Any],
) -> list[Filter]:
    """Translate a ``where(**kwargs)`` call into a list of filters."""
    out: list[Filter] = []
    for key, value in kwargs.items():
        if "__" in key:
            name, op = key.rsplit("__", 1)
        else:
            name, op = key, None
        out.append(_build_predicate(fields, name, op, value))
    return out


def _and_all(filters: Iterable[Filter]) -> Optional[Filter]:
    """Combine a list of filters with AND. Returns None if empty."""
    items = list(filters)
    if not items:
        return None
    out = items[0]
    for f in items[1:]:
        out = out & f
    return out


# ---------- query state ----------

@dataclass(frozen=True, slots=True, kw_only=True)
class _QueryState:
    filters: tuple[Filter, ...] = ()
    order: tuple[tuple[Filter, Direction], ...] = ()
    limit: Optional[int] = None
    offset: int = 0
    page_size: int = _DEFAULT_PAGE_SIZE


# ---------- base query ----------

class _BaseQuery(Generic[T]):
    """Shared chainable behaviour. Subclasses provide ``_FIELDS`` and the
    list/mutation methods specific to the resource."""

    _FIELDS: dict[str, Filter] = {}

    __slots__ = ("_ctx", "_state")

    def __init__(self, ctx: "_QueryContext", state: Optional[_QueryState] = None) -> None:
        self._ctx = ctx
        self._state = state or _QueryState()

    # ---- chainable builders ----

    def _replace(self, **patch: Any):
        return type(self)(self._ctx, replace(self._state, **patch))

    def where(self, **kwargs: Any):
        """AND new predicates with whatever's already there."""
        new_filters = self._state.filters + tuple(_filters_from_kwargs(self._FIELDS, kwargs))
        return self._replace(filters=new_filters)

    def where_expr(self, expr: Filter):
        """AND a raw upstream filter expression with the existing predicates.

        Use for OR / complex predicates the kwargs can't express:
        ``q.where_expr((TaskFieldFilter.STATUS == ERROR) | (TaskFieldFilter.STATUS == TIMEOUT))``.
        """
        return self._replace(filters=self._state.filters + (expr,))

    def order_by(self, *fields: str):
        """Sort by field(s). Prefix with ``-`` for descending."""
        order: list[tuple[Filter, Direction]] = []
        for f in fields:
            if f.startswith("-"):
                name, direction = f[1:], Direction.DESC
            elif f.startswith("+"):
                name, direction = f[1:], Direction.ASC
            else:
                name, direction = f, Direction.ASC
            if name not in self._FIELDS:
                raise ValueError(
                    f"can't sort by {name!r}; valid fields: "
                    f"{', '.join(sorted(self._FIELDS))}"
                )
            order.append((self._FIELDS[name], direction))
        return self._replace(order=tuple(order))

    def limit(self, n: int):
        if n < 0:
            raise ValueError("limit must be ≥ 0")
        return self._replace(limit=n)

    def offset(self, n: int):
        if n < 0:
            raise ValueError("offset must be ≥ 0")
        return self._replace(offset=n)

    def page_size(self, n: int):
        if n <= 0:
            raise ValueError("page_size must be > 0")
        return self._replace(page_size=n)

    # ---- filter / sort assembly ----

    def _filter(self) -> Optional[Filter]:
        return _and_all(self._state.filters)

    def _sort_args(self) -> tuple[Optional[Filter], Direction]:
        if not self._state.order:
            return (None, Direction.ASC)
        first = self._state.order[0]
        if len(self._state.order) > 1:
            log.debug(
                "multi-field sort requested; upstream supports only one — "
                "applying the first key only",
                fields=[(f.field_name if hasattr(f, "field_name") else "?") for f, _ in self._state.order],
            )
        return first

    # ---- terminals (subclasses do the actual work) ----

    def list(self) -> list[T]:
        return list(self._iter_pages(stop_at_limit=True))

    async def list_async(self) -> list[T]:
        return await anyio.to_thread.run_sync(self.list)

    def first(self) -> Optional[T]:
        for item in self._replace(limit=1)._iter_pages(stop_at_limit=True):
            return item
        return None

    async def first_async(self) -> Optional[T]:
        return await anyio.to_thread.run_sync(self.first)

    def count(self) -> int:
        # Default: ask upstream for one page just to read ``total``. Subclasses
        # can override with a cheaper RPC where one exists (e.g. tasks).
        total, _ = self._fetch_page(0, 1)
        return total

    async def count_async(self) -> int:
        return await anyio.to_thread.run_sync(self.count)

    # ---- iteration ----

    def __iter__(self) -> Iterator[T]:
        yield from self._iter_pages(stop_at_limit=True)

    async def __aiter__(self) -> AsyncIterator[T]:
        # Keep semantics simple: fetch each page on a worker thread and
        # yield items synchronously. For very large result sets a more
        # streaming form would help; revisit when there's a use case.
        for item in await anyio.to_thread.run_sync(
            lambda: list(self._iter_pages(stop_at_limit=True))
        ):
            yield item

    # ---- pagination plumbing ----

    def _iter_pages(self, *, stop_at_limit: bool) -> Iterator[T]:
        page_size = self._state.page_size
        # Translate offset+limit into page-arithmetic. Upstream paginates
        # by zero-indexed page; honour the offset by skipping items in
        # the first page we pull.
        emitted = 0
        offset = self._state.offset
        # Compute starting page so we don't fetch the same skipped items
        # for huge offsets.
        start_page = offset // page_size
        skip_in_first = offset - (start_page * page_size)
        page = start_page

        while True:
            total, items = self._fetch_page(page, page_size)
            if not items:
                return
            if skip_in_first:
                items = items[skip_in_first:]
                skip_in_first = 0
            for it in items:
                yield it
                emitted += 1
                if stop_at_limit and self._state.limit is not None and emitted >= self._state.limit:
                    return
            if (page + 1) * page_size >= total:
                return
            page += 1
            if emitted >= _MAX_LIST_ITEMS:
                log.warning(
                    "iteration safety cap hit",
                    cap=_MAX_LIST_ITEMS,
                    hint="use .limit() / .page_size() to paginate explicitly",
                )
                return

    def _fetch_page(self, page: int, page_size: int) -> tuple[int, list[T]]:
        raise NotImplementedError


# ---------- context ----------

@dataclass(slots=True)
class _QueryContext:
    """Holds the gRPC clients + an optional session scope."""

    tasks: ArmoniKTasks
    sessions: ArmoniKSessions
    results: ArmoniKResults
    partitions: ArmoniKPartitions
    # When set, scoped queries (session.tasks / session.results) AND
    # this filter into every list call so callers don't have to.
    scoped_session_id: Optional[str] = None


def _make_context(channel: Any, *, scoped_session_id: Optional[str] = None) -> _QueryContext:
    return _QueryContext(
        tasks=ArmoniKTasks(channel),
        sessions=ArmoniKSessions(channel),
        results=ArmoniKResults(channel),
        partitions=ArmoniKPartitions(channel),
        scoped_session_id=scoped_session_id,
    )


# ---------- TaskQuery ----------

class TaskQuery(_BaseQuery[TaskInfo]):
    """Query / mutate tasks. Bound either to a client (cluster-wide) or
    to a session (auto-scoped to that ``session_id``)."""

    _FIELDS = _TASK_FIELDS

    def _filter(self) -> Optional[Filter]:
        f = super()._filter()
        if self._ctx.scoped_session_id is None:
            return f
        scope = TaskFieldFilter.SESSION_ID == self._ctx.scoped_session_id
        return scope if f is None else (scope & f)

    def _fetch_page(self, page: int, page_size: int) -> tuple[int, list[TaskInfo]]:
        sort_field, sort_dir = self._sort_args()
        kwargs: dict[str, Any] = dict(
            task_filter=self._filter(),
            page=page,
            page_size=page_size,
            sort_direction=sort_dir,
            with_errors=True,
        )
        if sort_field is not None:
            kwargs["sort_field"] = sort_field
        total, items = self._ctx.tasks.list_tasks(**kwargs)
        return total, [TaskInfo.from_armonik(t) for t in items]

    def count(self) -> int:
        # Tasks have a dedicated count RPC that returns per-status totals;
        # for a generic count we still want a single number.
        # Use the page-of-one trick to read upstream's ``total``.
        return super().count()

    # ---- mutations ----

    def cancel(self, *, chunk_size: int = 500) -> int:
        """Cancel every task matching the query. Returns the count cancelled."""
        ids = [t.id for t in self._iter_pages(stop_at_limit=True)]
        if not ids:
            return 0
        self._ctx.tasks.cancel_tasks(task_ids=ids, chunk_size=chunk_size)
        log.info("tasks cancelled", count=len(ids))
        return len(ids)

    async def cancel_async(self, *, chunk_size: int = 500) -> int:
        return await anyio.to_thread.run_sync(lambda: self.cancel(chunk_size=chunk_size))


# ---------- ResultQuery ----------

class ResultQuery(_BaseQuery[ResultInfo]):
    """Query / mutate results.

    Cluster-wide (``client.results``) lists everything visible to the
    caller and supports the upstream filter fields (``id``, ``status``).
    Session-scoped (``session.results``) is correct but slightly more
    expensive: ``list_results`` doesn't expose ``session_id`` as a
    filter field, so we enumerate the session's tasks first to collect
    their ``expected_output_ids``, then query results by those ids in
    chunks.
    """

    _FIELDS = _RESULT_FIELDS

    # Cap how big a single RESULT_ID OR-chain we'll send in one
    # ``list_results`` call. Each clause adds bytes to the protobuf
    # filter message; 100 keeps us well under the 4 MiB cap and matches
    # the polling-loop chunking convention from session.py.
    _RID_CHUNK = 100

    def _fetch_page(self, page: int, page_size: int) -> tuple[int, list[ResultInfo]]:
        sort_field, sort_dir = self._sort_args()

        if self._ctx.scoped_session_id is None:
            # Cluster-wide: straight pass-through.
            kwargs: dict[str, Any] = dict(
                result_filter=self._filter(),
                page=page,
                page_size=page_size,
                sort_direction=sort_dir,
            )
            if sort_field is not None:
                kwargs["sort_field"] = sort_field
            total, items = self._ctx.results.list_results(**kwargs)
            return total, [ResultInfo.from_armonik(r) for r in items]

        # Session-scoped: collect candidate result_ids from the session's
        # tasks, paginate the candidates, query each page by id-chain.
        candidates = self._scoped_candidate_ids()
        start = page * page_size
        end = start + page_size
        chunk_ids = candidates[start:end]
        if not chunk_ids:
            return len(candidates), []

        rid_field = ResultFieldFilter.RESULT_ID
        # Sub-chunk RESULT_ID OR-chains to stay under the protobuf cap.
        results: list[Any] = []
        for i in range(0, len(chunk_ids), self._RID_CHUNK):
            sub = chunk_ids[i : i + self._RID_CHUNK]
            id_filter = rid_field == sub[0]
            for r in sub[1:]:
                id_filter = id_filter | (rid_field == r)
            user_filter = self._filter()
            combined = id_filter if user_filter is None else (id_filter & user_filter)
            list_kwargs: dict[str, Any] = dict(
                result_filter=combined,
                page=0,
                page_size=len(sub),
                sort_direction=sort_dir,
            )
            if sort_field is not None:
                list_kwargs["sort_field"] = sort_field
            _t, items = self._ctx.results.list_results(**list_kwargs)
            results.extend(items)

        return len(candidates), [ResultInfo.from_armonik(r) for r in results]

    def _scoped_candidate_ids(self) -> list[str]:
        """Enumerate ``expected_output_ids`` for every task in the session.

        Memoised per-instance so a ``.list()`` followed by a ``.count()``
        doesn't re-walk the session twice.
        """
        # __slots__ on the base prohibits arbitrary attrs; use the state
        # dict-style by storing on the type's own __dict__ via a side cache.
        cached = _SCOPED_CANDIDATE_CACHE.get(id(self))
        if cached is not None:
            return cached
        sid = self._ctx.scoped_session_id
        assert sid is not None
        task_filter = TaskFieldFilter.SESSION_ID == sid
        ids: list[str] = []
        page = 0
        page_size = 500
        while True:
            total, items = self._ctx.tasks.list_tasks(
                task_filter=task_filter,
                page=page,
                page_size=page_size,
                with_errors=False,
            )
            for t in items:
                ids.extend(t.expected_output_ids or [])
            if (page + 1) * page_size >= total or not items:
                break
            page += 1
        _SCOPED_CANDIDATE_CACHE[id(self)] = ids
        return ids

    # ---- mutations ----

    def _require_scope(self, op: str) -> str:
        if self._ctx.scoped_session_id is None:
            raise ValueError(
                f"{op}() requires a session-scoped query — call from "
                f"``session.results`` rather than ``client.results``."
            )
        return self._ctx.scoped_session_id

    def delete(self, *, batch_size: int = 100) -> int:
        """Delete the bytes of every result matching the query.

        Operates only within a session scope (``session.results``).
        Returns the number of results whose data was deleted.
        """
        sid = self._require_scope("delete")
        ids = [r.id for r in self._iter_pages(stop_at_limit=True)]
        if not ids:
            return 0
        self._ctx.results.delete_result_data(
            result_ids=ids, session_id=sid, batch_size=batch_size
        )
        log.info("result data deleted", count=len(ids), session=sid)
        return len(ids)

    async def delete_async(self, *, batch_size: int = 100) -> int:
        return await anyio.to_thread.run_sync(lambda: self.delete(batch_size=batch_size))

    def download(self) -> dict[str, bytes]:
        """Download the bytes of every matching result.

        Returns ``{result_id: bytes}``. Sequential — one
        ``download_result_data`` per result. For huge result sets prefer
        :meth:`download_to` (saves to disk as it goes).
        """
        sid = self._require_scope("download")
        out: dict[str, bytes] = {}
        for r in self._iter_pages(stop_at_limit=True):
            out[r.id] = self._ctx.results.download_result_data(
                result_id=r.id, session_id=sid
            )
        log.info("results downloaded", count=len(out), session=sid)
        return out

    async def download_async(self) -> dict[str, bytes]:
        return await anyio.to_thread.run_sync(self.download)

    def download_to(
        self,
        directory: str | os.PathLike[str],
        *,
        filename: Optional[Callable[[ResultInfo], str]] = None,
    ) -> int:
        """Download each matching result to a file in ``directory``.

        ``filename(info) -> str`` lets you choose the on-disk name; the
        default is ``<result_id>.bin``. Returns the number of files
        written.
        """
        sid = self._require_scope("download_to")
        out_dir = Path(directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for r in self._iter_pages(stop_at_limit=True):
            data = self._ctx.results.download_result_data(
                result_id=r.id, session_id=sid
            )
            name = filename(r) if filename else f"{r.id}.bin"
            (out_dir / name).write_bytes(data)
            n += 1
        log.info("results downloaded to disk", count=n, dir=str(out_dir))
        return n

    async def download_to_async(
        self,
        directory: str | os.PathLike[str],
        *,
        filename: Optional[Callable[[ResultInfo], str]] = None,
    ) -> int:
        return await anyio.to_thread.run_sync(
            lambda: self.download_to(directory, filename=filename)
        )


# ---------- SessionQuery ----------

class SessionQuery(_BaseQuery[SessionInfo]):
    """Query / mutate sessions. Cluster-wide; ignores any session scope
    on the context (a session can't filter itself)."""

    _FIELDS = _SESSION_FIELDS

    def _fetch_page(self, page: int, page_size: int) -> tuple[int, list[SessionInfo]]:
        sort_field, sort_dir = self._sort_args()
        kwargs: dict[str, Any] = dict(
            session_filter=self._filter(),
            page=page,
            page_size=page_size,
            sort_direction=sort_dir,
        )
        if sort_field is not None:
            kwargs["sort_field"] = sort_field
        total, items = self._ctx.sessions.list_sessions(**kwargs)
        return total, [SessionInfo.from_armonik(s) for s in items]

    # ---- mutations: each is per-session, so we iterate ----

    def _apply(self, op_name: str, fn: Callable[[str], Any]) -> int:
        n = 0
        for s in self._iter_pages(stop_at_limit=True):
            try:
                fn(s.id)
                n += 1
            except Exception as e:
                log.warning(f"{op_name} failed for one session", id=s.id, error=str(e))
        log.info(f"sessions {op_name}", count=n)
        return n

    def cancel(self) -> int:
        """Cancel every matching session. Returns the count succeeded."""
        return self._apply("cancelled", self._ctx.sessions.cancel_session)

    def pause(self) -> int:
        return self._apply("paused", self._ctx.sessions.pause_session)

    def resume(self) -> int:
        return self._apply("resumed", self._ctx.sessions.resume_session)

    def close(self) -> int:
        return self._apply("closed", self._ctx.sessions.close_session)

    def purge(self) -> int:
        return self._apply("purged", self._ctx.sessions.purge_session)

    def delete(self) -> int:
        return self._apply("deleted", self._ctx.sessions.delete_session)

    def stop_submission(self, *, client: bool = True, worker: bool = True) -> int:
        """Block further submissions on every matching session.

        ``client=True`` blocks user clients; ``worker=True`` blocks
        sub-task spawns from inside running tasks. Both default to True
        (full freeze).
        """
        return self._apply(
            "stop_submission",
            lambda sid: self._ctx.sessions.stop_submission_session(
                session_id=sid, client=client, worker=worker
            ),
        )

    # async siblings
    async def cancel_async(self) -> int:
        return await anyio.to_thread.run_sync(self.cancel)

    async def pause_async(self) -> int:
        return await anyio.to_thread.run_sync(self.pause)

    async def resume_async(self) -> int:
        return await anyio.to_thread.run_sync(self.resume)

    async def close_async(self) -> int:
        return await anyio.to_thread.run_sync(self.close)

    async def purge_async(self) -> int:
        return await anyio.to_thread.run_sync(self.purge)

    async def delete_async(self) -> int:
        return await anyio.to_thread.run_sync(self.delete)


# ---------- PartitionQuery ----------

class PartitionQuery(_BaseQuery[PartitionInfo]):
    """Query partitions. Read-only — partitions are managed via Terraform
    / Helm at deploy time, not from the SDK."""

    _FIELDS = _PARTITION_FIELDS

    def _fetch_page(self, page: int, page_size: int) -> tuple[int, list[PartitionInfo]]:
        sort_field, sort_dir = self._sort_args()
        kwargs: dict[str, Any] = dict(
            partition_filter=self._filter(),
            page=page,
            page_size=page_size,
            sort_direction=sort_dir,
        )
        if sort_field is not None:
            kwargs["sort_field"] = sort_field
        total, items = self._ctx.partitions.list_partitions(**kwargs)
        return total, [PartitionInfo.from_armonik(p) for p in items]
