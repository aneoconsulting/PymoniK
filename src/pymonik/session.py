"""Session — unit of work against an ArmoniK cluster (client-side).

Opens an ArmoniK session, owns the default task options, registers
in-flight futures, and runs a background poller thread that resolves
futures as results complete.

Submission, retry, and re-submission all go through the shared pipeline
in :mod:`pymonik._internal.submit`. This module's job is the
control-plane lifecycle (create / close / cancel session, run the
events stream, turn aborted results into typed errors) and the
plumbing that wires the pipeline to ArmoniK's gRPC clients.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import anyio
import grpc
from pymonik._internal._logging import get_logger
from armonik.client import ArmoniKEvents, ArmoniKResults, ArmoniKSessions, ArmoniKTasks
from armonik.common import EventTypes, Result, ResultStatus, TaskDefinition, TaskOptions
from armonik.common.events import ResultStatus as EvResultStatus
from armonik.common.events import ResultStatusUpdateEvent

import hashlib

import cloudpickle

import pymonik.hooks as hooks
from pymonik import blob as blob_mod
from pymonik._internal import _otel
from pymonik._internal.exec_cache import (
    ExecCache,
    ResultIndex,
    compute_cache_key,
    fn_identity,
)
from pymonik._internal.query import (
    ResultQuery,
    TaskQuery,
    _make_context,
)
from pymonik._internal.submit import SubmissionBackend, normalise_calls, submit_many
from pymonik.errors import TaskCancelled, TaskFailed
from pymonik.future import Future, FutureList
from pymonik.options import EMPTY, TaskOpts
from pymonik.task import Task, _current_session

if TYPE_CHECKING:
    from pymonik.client import PymonikClient

log = get_logger(__name__)

# How often the session poller scans for completed results (seconds).
_POLL_INTERVAL = 0.5

# Maximum pending result_ids to pack into a single ``list_results`` RPC.
# The filter is an OR chain over ``Result.result_id == <rid>`` predicates;
# once ``len(pending_ids)`` gets into the thousands the serialised request
# approaches gRPC's default 4 MiB cap. Chunk to stay well under.
_POLL_CHUNK = 500

# Default auto-spill threshold: args cloudpickled larger than this are
# uploaded as blobs and passed via data_dependencies. Chosen so tiny
# collections stay inline and typical numpy arrays / large dicts spill
# before they hit the gRPC default 4 MiB message cap.
_DEFAULT_SPILL_THRESHOLD = 256 * 1024


class Session:
    """An open ArmoniK session bound to a specific partition."""

    def __init__(
        self,
        client: "PymonikClient",
        partition: str | list[str] | tuple[str, ...],
        default_options: TaskOpts = EMPTY,
        *,
        use_events: bool = True,
        polling_interval: float = _POLL_INTERVAL,
        polling_chunk: int = _POLL_CHUNK,
        spill_threshold: int = _DEFAULT_SPILL_THRESHOLD,
        cache: ExecCache | None = None,
        attach_to: str | None = None,
    ) -> None:
        self._client = client
        # Normalise partitions: first element is the default; the full
        # list is what the session advertises to ArmoniK on create. When
        # attaching to an existing session, the partition list is
        # informational (used only for client-side per-task partition
        # validation); the cluster-side declaration was done on create.
        if isinstance(partition, str):
            self._partitions: tuple[str, ...] = (partition,)
        else:
            parts = tuple(partition)
            if not parts:
                raise ValueError("partition list cannot be empty")
            self._partitions = parts
        self._partition = self._partitions[0]
        self._default_opts = default_options
        self._use_events = use_events
        self._polling_interval = polling_interval
        self._polling_chunk = polling_chunk
        self._spill_threshold = spill_threshold
        self._cache = cache
        # Reuse index (key → existing result_id), colocated with the
        # value cache root. Present whenever caching infra is enabled.
        self._index: ResultIndex | None = (
            ResultIndex(cache.root) if cache is not None else None
        )
        # Existing session id we're attaching to. None = create a fresh
        # session on open. When attached, ``__exit__`` doesn't issue
        # ``close_session()`` — other consumers may still be using the
        # session and aren't ours to terminate.
        self._attach_to = attach_to

        self._session_id: str | None = None
        self._sessions: ArmoniKSessions | None = None
        self._tasks: ArmoniKTasks | None = None
        self._results: ArmoniKResults | None = None
        self._events: ArmoniKEvents | None = None

        self._pending: dict[str, Future[Any]] = {}  # result_id -> future
        # Within-session content-addressable blob cache. Keyed by SHA-256
        # hex of the bytes; value is the result_id.
        self._blob_cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._runner: threading.Thread | None = None
        self._ctx_token: Any = None
        # Long-lived OTel span covering the whole ``with`` block so
        # every submit / blob upload / future wait inside nests into
        # one trace instead of becoming its own root.
        self._otel_session_span: Any = None
        self._otel_session_token: Any = None
        # Set after ``cancel()``: the cluster has already terminated the
        # session, so we skip ``close_session()`` in ``__exit__`` to avoid
        # a noisy warning about "state that cannot be closed".
        self._cancelled: bool = False

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("session is not open")
        return self._session_id

    @property
    def partition(self) -> str:
        """The session's *default* partition (first of the partition list)."""
        return self._partition

    @property
    def partitions(self) -> tuple[str, ...]:
        """All partitions this session can route into."""
        return self._partitions

    # ---- introspection (session-scoped) ----

    def _qctx(self):
        if self._client._channel is None:
            raise RuntimeError("session is not open")
        return _make_context(
            self._client._channel,
            scoped_session_id=self.session_id,
        )

    @property
    def tasks(self) -> TaskQuery:
        """Tasks in this session (auto-scoped via ``session_id``)."""
        return TaskQuery(self._qctx())

    @property
    def results(self) -> ResultQuery:
        """Results in this session.

        Mutation verbs (``delete()`` / ``download()`` / ``download_to()``)
        operate on this session.
        """
        return ResultQuery(self._qctx())

    # ---- lifecycle (shared between sync and async) ----

    def _open_resources(self) -> None:
        """Blocking setup: armonik clients, session creation, completion thread.

        Called from sync ``__enter__`` directly, and from async
        ``__aenter__`` via ``anyio.to_thread.run_sync`` so the event loop
        isn't blocked on gRPC.
        """
        channel = self._client._channel
        assert channel is not None, "client channel is not open"
        self._sessions = ArmoniKSessions(channel)
        self._tasks = ArmoniKTasks(channel)
        self._results = ArmoniKResults(channel)
        self._events = ArmoniKEvents(channel)

        # Make the OTel auto-detection run before we open the long span,
        # otherwise the span goes to a no-op tracer.
        _otel.setup()

        # Open the long-lived session span. Everything else inside the
        # ``with`` block — submits, blob uploads, future waits, the
        # session.open RPC sub-span below — nests under this so the
        # whole thing shows up as one trace in Jaeger.
        self._otel_session_span, self._otel_session_token = _otel.start_long_span(
            "pymonik.session",
            attrs={
                "pymonik.partitions": ",".join(self._partitions),
                "pymonik.completion": "events" if self._use_events else "poll",
                "pymonik.attached": self._attach_to is not None,
            },
            kind="client",
        )

        if self._attach_to is not None:
            # Attaching: skip create_session, trust the user-supplied id.
            # We don't validate the id exists up front — the first
            # submission RPC will fail clearly enough if it doesn't.
            self._session_id = self._attach_to
            if self._otel_session_span is not None:
                self._otel_session_span.set_attribute(
                    "pymonik.session_id", self._session_id
                )
            log.info(
                "session attached",
                session_id=self._session_id,
                partitions=list(self._partitions),
                completion="events" if self._use_events else "poll",
            )
        else:
            default_armonik = self._default_opts.to_armonik(default_partition=self._partition)
            with _otel.start_span(
                "pymonik.session.open",
                attrs={
                    "pymonik.partitions": ",".join(self._partitions),
                    "pymonik.completion": "events" if self._use_events else "poll",
                },
                kind="client",
            ) as span:
                self._session_id = self._sessions.create_session(
                    default_task_options=default_armonik,
                    partition_ids=list(self._partitions),
                )
                if span is not None:
                    span.set_attribute("pymonik.session_id", self._session_id)
                if self._otel_session_span is not None:
                    self._otel_session_span.set_attribute(
                        "pymonik.session_id", self._session_id
                    )
            log.info(
                "session opened",
                session_id=self._session_id,
                partitions=list(self._partitions),
                completion="events" if self._use_events else "poll",
            )

        if hooks.active():
            hooks.emit(
                hooks.SessionOpened,
                session_id=self._session_id,
                partitions=tuple(self._partitions),
                attached=self._attach_to is not None,
            )

        # Copy the current ContextVars (including OTel's active span) into
        # the runner thread so any RPC it makes — Events.GetEvents,
        # Tasks.list_results during polling, Results.DownloadResultData
        # on completion — chains under ``pymonik.session`` instead of
        # opening a new trace root.
        import contextvars

        target = self._events_loop if self._use_events else self._poll_loop
        ctx = contextvars.copy_context()
        self._runner = threading.Thread(
            target=lambda: ctx.run(target),
            name=f"pymonik-{self._session_id}",
            daemon=True,
        )
        self._runner.start()

    def _close_resources(self) -> None:
        """Blocking teardown: stop thread, fail pending, close session."""
        self._stop.set()
        if self._runner is not None and self._runner.is_alive():
            # The events stream is blocking on a server-push; closing the
            # session below (or the channel on client exit) breaks it out.
            self._runner.join(timeout=2.0)

        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            fut._resolve_error(TaskCancelled(fut.task_id))

        # Don't close the session on exit when we attached — it isn't
        # ours to terminate. Other consumers may still be using it.
        if (
            self._session_id
            and self._sessions
            and not self._cancelled
            and self._attach_to is None
        ):
            try:
                self._sessions.close_session(self._session_id)
            except Exception as e:
                log.warning("close_session failed", error=str(e))

        # End the long-lived OTel session span last so its duration
        # covers everything else.
        if self._otel_session_span is not None or self._otel_session_token is not None:
            _otel.end_long_span(self._otel_session_span, self._otel_session_token)
            self._otel_session_span = None
            self._otel_session_token = None

        if self._session_id is not None and hooks.active():
            hooks.emit(
                hooks.SessionClosed,
                session_id=self._session_id,
                cancelled=self._cancelled,
            )

    # ---- context manager (sync) ----

    def __enter__(self) -> "Session":
        self._open_resources()
        self._ctx_token = _current_session.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._close_resources()
        finally:
            if self._ctx_token is not None:
                _current_session.reset(self._ctx_token)
                self._ctx_token = None

    # ---- context manager (async) ----

    async def __aenter__(self) -> "Session":
        # gRPC calls are blocking — run on a worker thread so we don't stall
        # the event loop while the control plane creates our session.
        await anyio.to_thread.run_sync(self._open_resources)
        self._ctx_token = _current_session.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await anyio.to_thread.run_sync(self._close_resources)
        finally:
            if self._ctx_token is not None:
                _current_session.reset(self._ctx_token)
                self._ctx_token = None

    # ---- client-side retry ----

    def _schedule_retry(self, fut: Future[Any], *, attempt: int) -> None:
        """Sleep the backoff, then re-spawn the task underlying ``fut``.

        Runs on a daemon thread so the events / poll loop isn't blocked
        while we wait. Updates ``fut`` in place — the user's reference is
        rewired to the new task / result_id atomically.
        """
        rs = fut._retry_state
        assert rs is not None
        task, args, kwargs, _max_retries, _on_types, backoff_fn = rs
        delay = max(0.0, float(backoff_fn(attempt - 1)))
        log.info(
            "task retrying",
            task=task.name,
            attempt=attempt,
            delay_s=round(delay, 3),
            old_task_id=fut.task_id,
        )

        def _run():
            try:
                if delay > 0.0:
                    if self._stop.wait(timeout=delay):
                        # Session was torn down during backoff.
                        return
                self._resubmit_for_retry(fut, task, args, kwargs)
            except Exception as e:
                # If the resubmit itself blows up, fail the public future
                # so the user sees a real error instead of hanging.
                log.error("retry resubmit failed", task=task.name, error=str(e))
                fut._error = TaskFailed(fut.task_id, f"retry resubmit failed: {e!r}")
                fut._done.set()
                fut._wake_async()

        # Same context-propagation rationale as ``_open_resources``:
        # the resubmit thread issues gRPC calls that should chain under
        # the session's trace.
        import contextvars

        retry_ctx = contextvars.copy_context()
        threading.Thread(
            target=lambda: retry_ctx.run(_run),
            name=f"pymonik-retry-{fut.task_id[:8]}",
            daemon=True,
        ).start()

    def _resubmit_for_retry(
        self,
        fut: Future[Any],
        task: Task[Any, Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Re-submit one task and rewire ``fut`` to the new task / output.

        Goes through the same shared pipeline as :meth:`_submit_many`,
        with ``existing_future=fut`` so the user-visible Future is mutated
        in place (no new object). The retry state is preserved so further
        failures can retry again until the budget is exhausted.
        """
        backend = _ClientBackend(self)

        def make_future(*_a, **_k) -> Future[Any]:  # unused for the retry path
            raise AssertionError("future_factory should not be called when existing_future is set")

        def register(output_ids: list[str], registered_fut: Any) -> None:
            with self._lock:
                # Old result_id was already popped from _pending by
                # _resolve_result before our error handler ran; just
                # register the new one. Retry path is single-output.
                self._pending[output_ids[0]] = registered_fut

        submit_many(
            task=task,
            calls=[(args, kwargs)],
            backend=backend,
            blob_uploader=self._upload_blob,
            spill_threshold=self._spill_threshold,
            default_opts=self._default_opts,
            partition=self._partition,
            future_factory=make_future,
            on_submitted=register,
            apply_retry_policy=False,  # already on the future from the original submit
            existing_future=fut,
            attempt=fut._retry_attempt + 1,
        )
        log.info(
            "task retried",
            task=task.name,
            attempt=fut._retry_attempt,
            new_task_id=fut.task_id,
        )

    # ---- cancellation ----

    def _cancel_future(self, fut: Future[Any]) -> None:
        """Cancel one task on the cluster and resolve its future locally.

        Called by :meth:`Future.cancel`. Uses ArmoniK's ``CancelTasks``.

        Ordering matters: resolve locally **before** issuing the RPC.
        Otherwise the events stream can deliver a ``RESULT_STATUS_UPDATE``
        with ``ABORTED`` while we're still round-tripping to the control
        plane, beating us to ``_resolve_error`` and leaving the future
        with ``TaskFailed("result aborted")`` instead of ``TaskCancelled``.
        """
        assert self._tasks is not None
        with self._lock:
            self._pending.pop(fut.result_id, None)
        fut._resolve_error(TaskCancelled(fut.task_id))
        try:
            self._tasks.cancel_tasks(task_ids=[fut.task_id])
        except Exception as e:
            log.warning("cancel_tasks failed", task_id=fut.task_id, error=str(e))

    def cancel(self) -> None:
        """Cancel this session and every in-flight task it holds.

        Marks every pending future as :class:`TaskCancelled` locally so
        callers blocking on them wake up; the cluster finishes the wind-down
        asynchronously. Same race-with-events-stream ordering as
        :meth:`_cancel_future` — resolve first, then RPC.
        """
        assert self._sessions is not None
        # Flip the flag *under the lock*, before we resolve the futures.
        # Otherwise the main thread wakes on its .result() before we get to
        # `self._cancelled = True` and hits ``__exit__`` → ``close_session``
        # while _cancelled is still False (race with cancel_session).
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            self._cancelled = True
        for fut in pending:
            fut._resolve_error(TaskCancelled(fut.task_id))
        try:
            self._sessions.cancel_session(self.session_id)
        except Exception as e:
            log.warning("cancel_session failed", error=str(e))
        log.info("session cancelled", session_id=self.session_id, cancelled_count=len(pending))

    def pause(self) -> None:
        """Pause submissions on this session (Pause RPC).

        New tasks can't be submitted while paused. In-flight tasks
        continue. Call :meth:`resume` to lift the pause.
        """
        assert self._sessions is not None
        self._sessions.pause_session(self.session_id)
        log.info("session paused", session_id=self.session_id)

    def resume(self) -> None:
        """Resume submissions after a previous :meth:`pause`."""
        assert self._sessions is not None
        self._sessions.resume_session(self.session_id)
        log.info("session resumed", session_id=self.session_id)

    def stop_submission(self, *, client: bool = True, worker: bool = True) -> None:
        """Block further submissions on this session.

        ``client=True`` blocks user clients; ``worker=True`` blocks
        sub-task spawns from inside running tasks. Both default to True
        (full freeze). Unlike :meth:`pause` this is one-way: you can't
        un-stop submissions, only cancel and re-create the session.
        """
        assert self._sessions is not None
        self._sessions.stop_submission_session(
            session_id=self.session_id, client=client, worker=worker
        )
        log.info(
            "session submissions stopped",
            session_id=self.session_id,
            client=client,
            worker=worker,
        )

    # ---- blob upload (content-addressable, within-session dedup) ----

    def _upload_blob(self, data: bytes) -> str:
        """Upload ``data`` (cloudpickled object bytes or raw file bytes).

        Deduplicates within the session by SHA-256 content hash — passing
        the same bytes twice returns the same result_id and skips the
        network round-trip.
        """
        assert self._results is not None
        h = blob_mod.content_hash(data)
        with self._lock:
            cached = self._blob_cache.get(h)
        if cached is not None:
            return cached

        with _otel.start_span(
            "pymonik.blob.upload",
            attrs={"pymonik.bytes": len(data), "pymonik.hash": h[:16]},
            kind="client",
        ):
            name = f"{self.session_id}__blob__{h[:16]}"
            result_map = self._results.create_results(
                results_data={name: data},
                session_id=self.session_id,
            )
            rid = result_map[name].result_id
        with self._lock:
            cached2 = self._blob_cache.get(h)
            if cached2 is not None:
                return cached2
            self._blob_cache[h] = rid
        log.info("blob uploaded", hash=h[:16], size=len(data), result_id=rid)
        return rid

    # ---- submission ----

    def _submit_one(
        self,
        task: Task[Any, Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Future[Any]:
        futures = self._submit_many(task, [(args, kwargs)])
        return futures[0]

    def _submit_many(
        self,
        task: Task[Any, Any],
        calls: list[Any],
    ) -> FutureList[Any]:
        """Submit N invocations of one task via the shared pipeline.

        Cache-active calls (``self._cache is not None and
        task.opts.cache is True``) short-circuit on hit: a pre-resolved
        Future is returned without any RPC. Misses go through the normal
        pipeline and are tagged with their cache key so
        :meth:`_resolve_result` can write them back when they land.
        """
        normalised = normalise_calls(calls)

        # Cache filter pass.
        reused, miss_idxs, keys = self._cache_classify(task, normalised)

        # Submit only the misses.
        miss_calls = [normalised[i] for i in miss_idxs]
        if miss_calls:
            miss_futures = self._submit_through_pipeline(task, miss_calls)
        else:
            miss_futures = FutureList([])

        # Stitch back to original order; tag misses with their cache key
        # so a successful result is recorded in the index (and optionally
        # the local-value cache) when it lands.
        out: list[Future[Any]] = [None] * len(normalised)  # type: ignore[list-item]
        for i, (rid, key, owner) in reused.items():
            out[i] = Future._new_reused(self, rid, key, owner)
        for j, idx in enumerate(miss_idxs):
            fut = miss_futures[j]
            if idx in keys:
                fut._cache_key = keys[idx]
            out[idx] = fut

        return FutureList(out)

    def _cache_classify(
        self,
        task: Task[Any, Any],
        normalised: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> tuple[dict[int, tuple[str, str, str | None]], list[int], dict[int, str]]:
        """Decide reuse / miss / uncacheable for each call.

        Returns ``(reused, miss_idxs, keys)``:
        - ``reused``: ``{idx: (result_id, cache_key)}`` — an existing
          cluster result, validated COMPLETED, to bind a future to (no
          resubmission).
        - ``miss_idxs``: indices that go through submission.
        - ``keys``: ``{idx: cache_key}`` for cacheable misses — recorded
          in the index when their result completes. Indices absent from
          ``reused`` and ``keys`` are uncacheable.
        """
        eff = self._default_opts.merge(task.opts)
        if self._cache is None or self._index is None or eff.cache is not True:
            return {}, list(range(len(normalised))), {}

        import pymonik

        fn_id = fn_identity(task.func, cache_version=eff.cache_version)
        keys: dict[int, str] = {}
        # candidate result_id per cacheable call that has an index entry
        candidates: dict[int, str] = {}
        miss_idxs: list[int] = []

        for i, (args, kwargs) in enumerate(normalised):
            key = compute_cache_key(
                pymonik_version=pymonik.__version__,
                task_name=task.name,
                fn_id=fn_id,
                args=args,
                kwargs=kwargs,
            )
            if key is None:
                miss_idxs.append(i)  # uncacheable
                continue
            keys[i] = key
            entry = self._index.get(key)
            if entry is not None:
                candidates[i] = entry["result_id"]

        # Validate candidates against the cluster in one batch — only
        # reuse results that still exist and COMPLETED. Stale/evicted →
        # fall through to a normal submit (no retention guarantees).
        # ``valid`` maps result_id → owner_task_id (the producing task).
        valid = self._validate_results(set(candidates.values()))
        reused: dict[int, tuple[str, str, str | None]] = {}
        for i in range(len(normalised)):
            if i in candidates and candidates[i] in valid:
                rid = candidates[i]
                reused[i] = (rid, keys[i], valid[rid])
            elif i in keys:
                miss_idxs.append(i)  # cacheable miss (key recorded on success)
            # else: already in miss_idxs (uncacheable)

        miss_idxs.sort()
        if reused:
            log.info(
                "result reuse",
                task=task.name,
                reused=len(reused),
                misses=len(miss_idxs),
            )
        return reused, miss_idxs, keys

    def _validate_results(self, result_ids: set[str]) -> dict[str, str | None]:
        """Map each still-existing, COMPLETED ``result_id`` to its
        ``owner_task_id`` (the task that produced it). Results that are
        missing or not COMPLETED are absent from the returned map and so
        won't be reused."""
        if not result_ids or self._results is None:
            return {}
        from armonik.client import ResultFieldFilter
        from armonik.common import ResultStatus

        ids = list(result_ids)
        filt = None
        for rid in ids:
            cond = ResultFieldFilter.RESULT_ID == rid
            filt = cond if filt is None else (filt | cond)
        valid: dict[str, str | None] = {}
        try:
            _total, items = self._results.list_results(
                result_filter=filt, page=0, page_size=len(ids)
            )
            for r in items:
                if r.status == ResultStatus.COMPLETED:
                    valid[r.result_id] = getattr(r, "owner_task_id", None) or None
        except Exception as e:  # noqa: BLE001 — a validation failure → no reuse
            log.debug("result validation failed; treating as miss", error=str(e))
            return {}
        return valid

    def _submit_through_pipeline(
        self,
        task: Task[Any, Any],
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> FutureList[Any]:
        """Hand a (post-cache-filter) list of calls to the shared pipeline."""
        from pymonik.future import MultiResultHandle

        backend = _ClientBackend(self)
        multi_fields = task.multi_fields

        def make_future(
            task_id: str,
            output_ids: list[str],
            _args: tuple[Any, ...],
            _kwargs: dict[str, Any],
        ) -> Any:
            if multi_fields:
                field_to_future = {
                    field: Future(self, task_id=task_id, result_id=oid)
                    for field, oid in zip(multi_fields, output_ids)
                }
                return MultiResultHandle(self, task_id, field_to_future)
            return Future(self, task_id=task_id, result_id=output_ids[0])

        def register(output_ids: list[str], handle: Any) -> None:
            with self._lock:
                if multi_fields:
                    # Each output id keys into the same handle; the
                    # completion loop resolves whichever field's
                    # output id arrives. The MultiResultHandle holds
                    # the per-field Futures.
                    for field, oid in zip(multi_fields, output_ids):
                        self._pending[oid] = handle._field_to_future[field]
                else:
                    self._pending[output_ids[0]] = handle

        return submit_many(
            task=task,
            calls=calls,
            backend=backend,
            blob_uploader=self._upload_blob,
            spill_threshold=self._spill_threshold,
            default_opts=self._default_opts,
            partition=self._partition,
            future_factory=make_future,
            on_submitted=register,
            apply_retry_policy=True,
            attempt=1,
        )

    # ---- events stream (default) ----

    def _events_loop(self) -> None:
        """Run the ``Events.GetEvents`` server-stream and resolve futures.

        Stops when ``self._stop`` is set (on the next event) or when the
        stream errors out (channel close during shutdown).
        """
        assert self._events is not None

        def handler(_session_id, event_type, event) -> bool:
            if self._stop.is_set():
                return True  # break the stream
            if (
                event_type == EventTypes.RESULT_STATUS_UPDATE
                and isinstance(event, ResultStatusUpdateEvent)
            ):
                with self._lock:
                    known = event.result_id in self._pending
                if not known:
                    return False
                if event.status == EvResultStatus.COMPLETED:
                    self._resolve_result(event.result_id, ok=True)
                elif event.status == EvResultStatus.ABORTED:
                    self._resolve_result(event.result_id, ok=False)
            return False

        try:
            self._events.get_events(
                session_id=self.session_id,
                event_types=[EventTypes.RESULT_STATUS_UPDATE],
                event_handlers=[handler],
            )
        except grpc.RpcError as e:
            if not self._stop.is_set():
                log.warning("events stream terminated", error=str(e))
        except Exception as e:  # noqa: BLE001 — bg thread; surface for the user
            log.error("events loop failure", error=str(e))

    # ---- polling fallback ----

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(timeout=self._polling_interval):
                return
            try:
                self._poll_once()
            except Exception as e:
                log.warning("poll iteration failed", error=str(e))

    def _poll_once(self) -> None:
        with self._lock:
            pending_ids = list(self._pending.keys())
        if not pending_ids:
            return
        for i in range(0, len(pending_ids), self._polling_chunk):
            self._poll_chunk(pending_ids[i : i + self._polling_chunk])

    def _poll_chunk(self, chunk_ids: list[str]) -> None:
        assert self._results is not None
        filt = None
        for rid in chunk_ids:
            cond = Result.result_id == rid
            filt = cond if filt is None else filt | cond

        _total, items = self._results.list_results(
            result_filter=filt,
            page=0,
            page_size=len(chunk_ids),
        )

        for res in items:
            if res.status == ResultStatus.COMPLETED:
                self._resolve_result(res.result_id, ok=True)
            elif res.status == ResultStatus.ABORTED:
                self._resolve_result(res.result_id, ok=False)

    def _resolve_result(self, result_id: str, *, ok: bool) -> None:
        assert self._results is not None
        assert self._tasks is not None

        with self._lock:
            fut = self._pending.pop(result_id, None)
        if fut is None:
            return

        if not ok:
            # Extra RPC: fetch the task's worker-side error output so the
            # user sees something more useful than "result aborted". Failures
            # are the exception path; the extra call is worth the UX.
            msg = "result aborted"
            try:
                t = self._tasks.get_task(fut.task_id)
                if t.output is not None and getattr(t.output, "error", None):
                    msg = t.output.error
                elif getattr(t, "status_message", None):
                    msg = t.status_message
            except Exception as e:
                log.debug("get_task for error details failed", error=str(e))
            fut._resolve_error(TaskFailed(fut.task_id, msg))
            return

        # Record the reuse mapping (structural key → this result_id) so a
        # later run can reuse it instead of resubmitting. Done at
        # completion (success only), so the index never points at a
        # failed result.
        if self._index is not None and fut._cache_key is not None:
            try:
                self._index.put(fut._cache_key, result_id, self.session_id)
            except Exception as e:  # noqa: BLE001 — never block the happy path
                log.debug("index write failed", error=str(e))

        # Mark COMPLETED only — no download here (ADR-0013). The bytes
        # are fetched lazily by the future's .result()/await via
        # ``_materialize_result``, so intermediate pipeline results the
        # client never reads are never pulled to the client.
        fut._mark_completed()

    def _materialize_result(self, result_id: str) -> bytes:
        """Download a result's bytes on demand (called by ``Future``).

        Runs while the session/channel is open — the future blocks the
        caller until the bytes arrive.
        """
        assert self._results is not None
        return self._results.download_result_data(
            result_id=result_id,
            session_id=self.session_id,
        )


class _ClientBackend:
    """Control-plane SubmissionBackend.

    Adapts ``ArmoniKResults`` / ``ArmoniKTasks`` to the three primitives
    :func:`pymonik._internal.submit.submit_many` calls.
    """

    __slots__ = ("_s",)

    def __init__(self, session: "Session") -> None:
        self._s = session

    @property
    def session_id(self) -> str:
        return self._s.session_id

    @property
    def allowed_partitions(self) -> tuple[str, ...] | None:
        return self._s._partitions

    def allocate_outputs(self, names: list[str]) -> list[str]:
        assert self._s._results is not None
        m = self._s._results.create_results_metadata(
            result_names=names, session_id=self._s.session_id
        )
        return [m[n].result_id for n in names]

    def upload_payloads(self, named_data: dict[str, bytes]) -> dict[str, str]:
        assert self._s._results is not None
        m = self._s._results.create_results(
            results_data=named_data, session_id=self._s.session_id
        )
        return {n: r.result_id for n, r in m.items()}

    def submit(
        self,
        definitions: list[TaskDefinition],
        default_options: TaskOptions,
    ) -> list[str]:
        assert self._s._tasks is not None
        submitted = self._s._tasks.submit_tasks(
            session_id=self._s.session_id,
            tasks=definitions,
            default_task_options=default_options,
        )
        return [s.id for s in submitted]
