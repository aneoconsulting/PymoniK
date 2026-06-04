"""In-process backend for tests / examples / iteration loops.

``LocalCluster`` mimics :class:`pymonik.PymonikClient` minus the network:
tasks run in a thread pool, but the public surface (``@task``,
``.spawn``, ``.map``, ``Future``, ``await fut``, blobs, ``current()``,
``.cancel()``, retries) behaves the same so user code is portable.

Fidelity
--------
Submission goes through the same shared pipeline
(:func:`pymonik._internal.submit.submit_many`) as the real client:
``extract_deps`` rewrites Future / Blob / Materialize args into wire
refs, ``auto_spill`` handles oversize values, the
:class:`~pymonik.envelope.TaskEnvelope` is encoded with msgspec, and a
worker function on the thread pool decodes that envelope, looks up data
dependencies in a session-local dict, runs the function, and pickles
the result. The wire format is exercised end-to-end in-process — bugs
in envelope encoding, ref resolution, or auto-spill surface here the
same way they would on the cluster.

What's still local-only:

- No pod scheduling latency, no partition routing, no autoscaling.
- No worker isolation — everything shares the host process.
- ``max_retries`` (cluster-side, infra-failure retry) isn't emulated;
  client-side retries via ``@task(retry_on=...)`` work end-to-end via
  the same code path the real session uses.

Deadlock note: the executor uses a default of 16 threads. A pipeline
whose in-flight depth exceeds the pool can deadlock (every thread
blocked on a data dep whose computation needs another thread). Pass
``LocalCluster(max_workers=N)`` for deeper graphs.
"""

from __future__ import annotations

import contextvars
import hashlib
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import cloudpickle
from pymonik._internal._logging import get_logger
from armonik.common import TaskDefinition, TaskOptions

from pymonik import context as ctx_mod
from pymonik import envelope as env_mod
from pymonik._internal.exec_cache import ExecCache, compute_cache_key, default_cache_dir
from pymonik._internal.refs import auto_spill, extract_deps, resolve_refs
from pymonik._internal.submit import normalise_calls, submit_many
from pymonik.context import WorkerContext
from pymonik.errors import TaskCancelled, TaskFailed
from pymonik.future import Future, FutureList
from pymonik.options import EMPTY, TaskOpts
from pymonik.task import Task, _current_session

log = get_logger(__name__)


class _FakeTaskHandler:
    """Minimal duck-type for ``armonik.worker.TaskHandler`` — what
    :class:`WorkerContext` reads off it (``task_id`` / ``session_id``).
    """

    __slots__ = ("task_id", "session_id")

    def __init__(self, task_id: str, session_id: str) -> None:
        self.task_id = task_id
        self.session_id = session_id


class LocalCluster:
    """Drop-in for ``PymonikClient`` that runs tasks in a thread pool.

    Use exactly like the real client::

        with LocalCluster() as client:
            with client.session(partition="local") as s:
                assert add.spawn(2, 3).result() == 5

    Or async::

        async with LocalCluster() as client:
            async with client.session_async(partition="local") as s:
                assert await add.spawn(2, 3) == 5
    """

    def __init__(
        self,
        *,
        max_workers: int = 16,
        cache: bool | str | Path | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        self._cache: ExecCache | None
        if cache is None or cache is False:
            self._cache = None
        elif cache is True:
            self._cache = ExecCache(default_cache_dir())
        else:
            self._cache = ExecCache(Path(cache))
        if self._cache is not None:
            log.info("local exec cache enabled", root=str(self._cache.root))

    # ---- sync lifecycle ----

    def __enter__(self) -> "LocalCluster":
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="pymonik-local",
        )
        log.info("local cluster started", max_workers=self._max_workers, mode="sync")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def session(
        self,
        *,
        partition: str | list[str] | tuple[str, ...] = "local",
        default_options: TaskOpts | None = None,
        deps: list[str] | tuple[str, ...] | None = None,
        isolate: bool | None = None,
        index_url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "LocalSession":
        merged = default_options or EMPTY
        if (
            deps is not None
            or isolate is not None
            or index_url is not None
            or env is not None
        ):
            merged = merged.merge(
                TaskOpts(
                    deps=tuple(deps) if deps is not None else None,
                    isolate=isolate,
                    index_url=index_url,
                    env=dict(env) if env is not None else None,
                )
            )
        return LocalSession(
            self,
            partition=partition,
            default_options=merged,
            cache=self._cache,
        )

    # ---- async lifecycle ----

    async def __aenter__(self) -> "LocalCluster":
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="pymonik-local",
        )
        log.info("local cluster started", max_workers=self._max_workers, mode="async")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._executor is not None:
            await anyio.to_thread.run_sync(self._executor.shutdown)
            self._executor = None

    @asynccontextmanager
    async def session_async(
        self,
        *,
        partition: str | list[str] | tuple[str, ...] = "local",
        default_options: TaskOpts | None = None,
        deps: list[str] | tuple[str, ...] | None = None,
        isolate: bool | None = None,
        index_url: str | None = None,
        env: dict[str, str] | None = None,
    ):
        merged = default_options or EMPTY
        if (
            deps is not None
            or isolate is not None
            or index_url is not None
            or env is not None
        ):
            merged = merged.merge(
                TaskOpts(
                    deps=tuple(deps) if deps is not None else None,
                    isolate=isolate,
                    index_url=index_url,
                    env=dict(env) if env is not None else None,
                )
            )
        sess = LocalSession(
            self,
            partition=partition,
            default_options=merged,
            cache=self._cache,
        )
        async with sess:
            yield sess


class LocalSession:
    """In-process equivalent of :class:`pymonik.session.Session`.

    Same submission API; futures are real :class:`pymonik.Future` instances
    so ``.result()`` / ``await`` / ``.cancel()`` work with the same code.
    Submission goes through the same shared pipeline the cluster session
    uses, so envelope encoding and ref resolution are exercised here too.
    """

    def __init__(
        self,
        cluster: LocalCluster,
        partition: str | list[str] | tuple[str, ...],
        default_options: TaskOpts = EMPTY,
        *,
        cache: ExecCache | None = None,
    ) -> None:
        self._cluster = cluster
        if isinstance(partition, str):
            self._partitions: tuple[str, ...] = (partition,)
        else:
            parts = tuple(partition)
            if not parts:
                raise ValueError("partition list cannot be empty")
            self._partitions = parts
        self._partition = self._partitions[0]
        self._default_opts = default_options
        self._cache = cache
        self._session_id = f"local-{uuid.uuid4().hex[:8]}"

        self._pending: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}

        # Three buckets of bytes addressable by result_id:
        #   _payloads      — envelope bytes from upload_payloads
        #   _blob_bytes    — blob.upload + auto-spill bytes
        #   _result_bytes  — pickled return values from completed tasks
        # _result_events signals "bytes are now in _result_bytes or
        # _blob_bytes", so dispatcher threads waiting on a data dep can
        # block efficiently.
        self._payloads: dict[str, bytes] = {}
        self._blob_bytes: dict[str, bytes] = {}
        self._result_bytes: dict[str, bytes] = {}
        self._result_events: dict[str, threading.Event] = {}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._spill_threshold = 1 << 30  # ~1 GiB; effectively never spill locally
        self._ctx_token: Any = None
        # Long-lived OTel session span — same idea as the cluster Session:
        # everything inside the ``with`` block nests into one trace.
        self._otel_session_span: Any = None
        self._otel_session_token: Any = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def partition(self) -> str:
        return self._partition

    @property
    def partitions(self) -> tuple[str, ...]:
        return self._partitions

    # ---- context manager (sync) ----

    def __enter__(self) -> "LocalSession":
        self._ctx_token = _current_session.set(self)
        from pymonik._internal import _otel as _otel_mod

        _otel_mod.setup()
        self._otel_session_span, self._otel_session_token = _otel_mod.start_long_span(
            "pymonik.session",
            attrs={
                "pymonik.partitions": ",".join(self._partitions),
                "pymonik.session_id": self._session_id,
                "pymonik.local": True,
            },
            kind="client",
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._stop.set()
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for fut in pending:
                fut._resolve_error(TaskCancelled(fut.task_id))
        finally:
            if self._ctx_token is not None:
                _current_session.reset(self._ctx_token)
                self._ctx_token = None
            if self._otel_session_span is not None or self._otel_session_token is not None:
                from pymonik._internal import _otel as _otel_mod

                _otel_mod.end_long_span(self._otel_session_span, self._otel_session_token)
                self._otel_session_span = None
                self._otel_session_token = None

    # ---- context manager (async) ----

    async def __aenter__(self) -> "LocalSession":
        self._ctx_token = _current_session.set(self)
        from pymonik._internal import _otel as _otel_mod

        _otel_mod.setup()
        self._otel_session_span, self._otel_session_token = _otel_mod.start_long_span(
            "pymonik.session",
            attrs={
                "pymonik.partitions": ",".join(self._partitions),
                "pymonik.session_id": self._session_id,
                "pymonik.local": True,
            },
            kind="client",
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            self._stop.set()
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for fut in pending:
                fut._resolve_error(TaskCancelled(fut.task_id))
        finally:
            if self._ctx_token is not None:
                _current_session.reset(self._ctx_token)
                self._ctx_token = None
            if self._otel_session_span is not None or self._otel_session_token is not None:
                from pymonik._internal import _otel as _otel_mod

                _otel_mod.end_long_span(self._otel_session_span, self._otel_session_token)
                self._otel_session_span = None
                self._otel_session_token = None

    # ---- blob upload (in-memory, content-hash dedup like Session) ----

    def _upload_blob(self, data: bytes) -> str:
        from pymonik import blob as blob_mod

        h = blob_mod.content_hash(data)
        rid = f"local-blob-{h[:16]}"
        with self._lock:
            if rid in self._blob_bytes:
                return rid
            self._blob_bytes[rid] = data
            ev = self._result_events.setdefault(rid, threading.Event())
            ev.set()
        return rid

    # ---- submission ----

    def _submit_one(
        self,
        task: Task[Any, Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        return self._submit_many(task, [(args, kwargs)])[0]

    def _submit_many(
        self,
        task: Task[Any, Any],
        calls: list[Any],
    ) -> FutureList[Any]:
        normalised = normalise_calls(calls)
        cached_hits, miss_idxs, keys = self._cache_classify(task, normalised)

        miss_calls = [normalised[i] for i in miss_idxs]
        if miss_calls:
            miss_futures = self._submit_through_pipeline(task, miss_calls)
        else:
            miss_futures = FutureList([])

        out: list[Future[Any]] = [None] * len(normalised)  # type: ignore[list-item]
        for i, raw in cached_hits.items():
            out[i] = Future._new_cached(self, raw)
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
    ) -> tuple[dict[int, bytes], list[int], dict[int, str]]:
        if self._cache is None or task.opts.cache is not True:
            return {}, list(range(len(normalised))), {}

        import pymonik

        fn_pickle_hash = hashlib.sha256(cloudpickle.dumps(task.func)).digest()
        cached_hits: dict[int, bytes] = {}
        miss_idxs: list[int] = []
        keys: dict[int, str] = {}
        for i, (args, kwargs) in enumerate(normalised):
            key = compute_cache_key(
                pymonik_version=pymonik.__version__,
                task_name=task.name,
                function_pickle_hash=fn_pickle_hash,
                args=args,
                kwargs=kwargs,
            )
            if key is None:
                miss_idxs.append(i)
                continue
            try:
                cached_hits[i] = self._cache.get_bytes(key)
                log.info("cache hit (local)", task=task.name, key=key[:16])
            except KeyError:
                miss_idxs.append(i)
                keys[i] = key
        if cached_hits:
            log.info(
                "cache batch summary (local)",
                task=task.name,
                hits=len(cached_hits),
                misses=len(miss_idxs),
            )
        return cached_hits, miss_idxs, keys

    def _submit_through_pipeline(
        self,
        task: Task[Any, Any],
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> FutureList[Any]:
        from pymonik.future import MultiResultHandle

        backend = _LocalBackend(self)
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

        def register_and_launch(output_ids: list[str], handle: Any) -> None:
            with self._lock:
                if multi_fields:
                    for field, oid in zip(multi_fields, output_ids):
                        self._pending[oid] = handle._field_to_future[field]
                else:
                    self._pending[output_ids[0]] = handle
            # The backend launches the dispatcher keyed by the *primary*
            # output id (first of the group). The dispatcher then writes
            # to all output ids in the group.
            backend._launch_for(output_ids)

        return submit_many(
            task=task,
            calls=calls,
            backend=backend,
            blob_uploader=self._upload_blob,
            spill_threshold=self._spill_threshold,
            default_opts=self._default_opts,
            partition=self._partition,
            future_factory=make_future,
            on_submitted=register_and_launch,
            apply_retry_policy=True,
            attempt=1,
        )

    # ---- retry path (same hooks Session uses) ----

    def _schedule_retry(self, fut: Future[Any], *, attempt: int) -> None:
        rs = fut._retry_state
        assert rs is not None
        task, args, kwargs, _max, _on, backoff_fn = rs
        delay = max(0.0, float(backoff_fn(attempt - 1)))
        log.info(
            "task retrying (local)",
            task=task.name,
            attempt=attempt,
            delay_s=round(delay, 3),
            old_task_id=fut.task_id,
        )

        def _run():
            if delay > 0.0 and self._stop.wait(timeout=delay):
                return
            backend = _LocalBackend(self)

            def make_future(*_a, **_k):
                raise AssertionError("future_factory not used with existing_future")

            def register_and_launch(output_ids: list[str], registered_fut: Any) -> None:
                with self._lock:
                    self._pending[output_ids[0]] = registered_fut
                backend._launch_for(output_ids)

            submit_many(
                task=task,
                calls=[(args, kwargs)],
                backend=backend,
                blob_uploader=self._upload_blob,
                spill_threshold=self._spill_threshold,
                default_opts=self._default_opts,
                partition=self._partition,
                future_factory=make_future,
                on_submitted=register_and_launch,
                apply_retry_policy=False,
                existing_future=fut,
                attempt=fut._retry_attempt + 1,
            )

        retry_ctx = contextvars.copy_context()
        threading.Thread(
            target=lambda: retry_ctx.run(_run),
            name=f"pymonik-local-retry-{fut.task_id[-8:]}",
            daemon=True,
        ).start()

    # ---- cancellation ----

    def _cancel_future(self, fut: Future[Any]) -> None:
        with self._lock:
            self._pending.pop(fut.result_id, None)
            ev = self._cancel_events.pop(fut.result_id, None)
            # Wake any data-dep waiters with no bytes (they'll see the
            # error path).
            rev = self._result_events.get(fut.result_id)
        if ev is not None:
            ev.set()
        if rev is not None:
            rev.set()
        fut._resolve_error(TaskCancelled(fut.task_id))

    def cancel(self) -> None:
        """Cancel this in-process session. Same shape as Session.cancel."""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            cancel_events = list(self._cancel_events.values())
            self._cancel_events.clear()
            result_events = list(self._result_events.values())
        for ev in cancel_events:
            ev.set()
        for ev in result_events:
            ev.set()
        for fut in pending:
            fut._resolve_error(TaskCancelled(fut.task_id))

    # LocalCluster has no real session-lifecycle RPCs, but we mirror the
    # cluster Session's verb set so the same code runs in both places.
    # pause/resume/stop_submission are no-ops locally.

    def pause(self) -> None:  # pragma: no cover - in-process no-op
        log.info("local pause (no-op)", session=self.session_id)

    def resume(self) -> None:  # pragma: no cover
        log.info("local resume (no-op)", session=self.session_id)

    def stop_submission(self, *, client: bool = True, worker: bool = True) -> None:  # pragma: no cover
        log.info(
            "local stop_submission (no-op)",
            session=self.session_id,
            client=client,
            worker=worker,
        )

    # ---- dispatcher (worker-equivalent for one task) ----

    def _write_result_bytes(self, output_id: str, pickled: bytes) -> None:
        """Write resolved bytes for one output id and wake any waiters."""
        with self._lock:
            self._result_bytes[output_id] = pickled
            ev = self._result_events.get(output_id)
        if ev is not None:
            ev.set()

    def _submit_tail(
        self,
        promise: Any,
        *,
        expected_output_ids: list[str],
    ) -> str:
        """Submit a TailPromise to run with caller-supplied output ids.

        The local equivalent of :meth:`WorkerSession._submit_tail`. Builds
        an envelope for the promise's task, registers it as a dispatch
        keyed by the (parent's) primary output id — replacing the
        parent's already-completed dispatch entry — and schedules. The
        tail's dispatcher writes to ``expected_output_ids``; the parent's
        future, registered under ``expected_output_ids[0]``, resolves
        when the tail completes.
        """
        from pymonik._internal._otel import inject_context

        task = promise._task
        args = promise._args
        kwargs = promise._kwargs

        deps: list[str] = []
        args_rewritten = tuple(extract_deps(a, deps) for a in args)
        kwargs_rewritten = {k: extract_deps(v, deps) for k, v in kwargs.items()}
        args_rewritten = tuple(
            auto_spill(
                a, deps, upload_blob=self._upload_blob, threshold=self._spill_threshold
            )
            for a in args_rewritten
        )
        kwargs_rewritten = {
            k: auto_spill(
                v, deps, upload_blob=self._upload_blob, threshold=self._spill_threshold
            )
            for k, v in kwargs_rewritten.items()
        }

        merged_opts = task.opts
        env_dict = merged_opts.env or {}
        env_spec_obj = None
        if merged_opts.deps or env_dict:
            from pymonik.envelope import EnvSpec

            env_spec_obj = EnvSpec(
                deps=tuple(merged_opts.deps or ()),
                isolate=merged_opts.isolate if merged_opts.isolate is not None else False,
                index_url=merged_opts.index_url or "",
                env=tuple(sorted(env_dict.items())),
            )

        carrier: dict[str, str] = {}
        inject_context(carrier)

        from pymonik.envelope import TaskEnvelope

        envelope = TaskEnvelope(
            function_pickle=cloudpickle.dumps(task.func),
            args_pickle=cloudpickle.dumps((args_rewritten, kwargs_rewritten)),
            func_name=task.name,
            attempt=1,
            env_spec=env_spec_obj,
            otel_context=tuple(sorted(carrier.items())),
            multi_fields=task.multi_fields or (),
        )
        payload_bytes = env_mod.encode(envelope)

        new_task_id = f"local-tail-{uuid.uuid4().hex[:12]}"
        cancel_ev = threading.Event()
        with self._lock:
            self._cancel_events[expected_output_ids[0]] = cancel_ev

        executor = self._cluster._executor
        assert executor is not None, "LocalCluster is not running"
        executor.submit(
            self._dispatch,
            new_task_id,
            list(expected_output_ids),
            payload_bytes,
            sorted(set(deps)),
            cancel_ev,
        )
        log.info(
            "tail submitted (local)",
            func=task.name,
            child_task=new_task_id,
            expected_outputs=list(expected_output_ids),
        )
        return new_task_id

    def _dispatch_result(
        self,
        *,
        result: Any,
        envelope: Any,
        task_id: str,
        output_ids: list[str],
        fut: Any,
    ) -> None:
        """Map a user-function return onto local output writes / tail submits.

        Mirrors :func:`pymonik.worker._dispatch_result` for LocalCluster.
        """
        from pymonik.future import MultiResultHandle as _MRH
        from pymonik.multiresult import MultiResult, TailPromise

        multi_fields = envelope.multi_fields

        # ---- whole-task tail-call ----
        if isinstance(result, TailPromise):
            child_task = result._task
            child_multi = child_task.multi_fields or ()
            if multi_fields:
                if child_multi != multi_fields:
                    fut._resolve_error(
                        TaskFailed(
                            task_id,
                            f"tail-called task {child_task.name!r} declares "
                            f"{list(child_multi)}, parent declares "
                            f"{list(multi_fields)}",
                        )
                    )
                    return
            else:
                if child_multi:
                    fut._resolve_error(
                        TaskFailed(
                            task_id,
                            f"tail-called task {child_task.name!r} is multi-output "
                            f"but parent is single-output",
                        )
                    )
                    return
            self._submit_tail(
                result, expected_output_ids=output_ids
            )
            return

        # ---- multi-output return ----
        if isinstance(result, MultiResult):
            if not multi_fields:
                fut._resolve_error(
                    TaskFailed(
                        task_id,
                        "function returned MultiResult but task wasn't declared "
                        "multi-output (decoration didn't extract a schema).",
                    )
                )
                return
            returned = set(result.fields.keys())
            declared = set(multi_fields)
            if returned != declared:
                fut._resolve_error(
                    TaskFailed(
                        task_id,
                        f"MultiResult shape mismatch: declared {sorted(declared)}, "
                        f"returned {sorted(returned)}",
                    )
                )
                return

            field_to_oid = dict(zip(multi_fields, output_ids))
            for field, value in result.fields.items():
                oid = field_to_oid[field]
                if isinstance(value, TailPromise):
                    if value._task.multi_fields:
                        fut._resolve_error(
                            TaskFailed(
                                task_id,
                                f"field {field!r} delegates to multi-output task "
                                f"{value._task.name!r}; not supported",
                            )
                        )
                        return
                    # Each per-field tail submits its own dispatch with
                    # only that output id; the field's Future is already
                    # registered under `oid`, so the tail dispatch
                    # resolves it when the child writes.
                    self._submit_tail(
                        value, expected_output_ids=[oid]
                    )
                elif isinstance(value, Future):
                    fut._resolve_error(
                        TaskFailed(
                            task_id,
                            f"field {field!r} is a Future from .spawn() — "
                            f"use .tail() for delegation",
                        )
                    )
                    return
                elif isinstance(value, _MRH):
                    fut._resolve_error(
                        TaskFailed(
                            task_id,
                            f"field {field!r} is a MultiResultHandle; nested "
                            f"per-field access isn't supported",
                        )
                    )
                    return
                else:
                    pickled = cloudpickle.dumps(value)
                    self._write_result_bytes(oid, pickled)
                    field_fut = self._field_future_for(oid)
                    if field_fut is not None:
                        field_fut._resolve_ok(pickled)
            return

        # ---- plain single-output return ----
        if multi_fields:
            fut._resolve_error(
                TaskFailed(
                    task_id,
                    f"task declared multi-output {list(multi_fields)} but "
                    f"returned {type(result).__name__} (expected MultiResult)",
                )
            )
            return

        try:
            pickled = cloudpickle.dumps(result)
        except Exception as e:
            fut._resolve_error(
                TaskFailed(task_id, f"could not pickle result: {e!r}")
            )
            return

        self._write_result_bytes(output_ids[0], pickled)
        if self._cache is not None and fut._cache_key is not None:
            try:
                self._cache.put_bytes(fut._cache_key, pickled)
                log.info(
                    "cache stored (local)",
                    task_id=fut.task_id,
                    key=fut._cache_key[:16],
                    bytes=len(pickled),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("cache write failed", error=str(e))
        fut._resolve_ok(pickled)

    def _field_future_for(self, output_id: str) -> "Future[Any] | None":
        """Look up the per-field Future registered for this output id."""
        with self._lock:
            return self._pending.get(output_id)

    def _dispatch(
        self,
        task_id: str,
        output_ids: list[str],
        payload_bytes: bytes,
        data_dep_ids: list[str],
        cancel_ev: threading.Event,
    ) -> None:
        """Decode the envelope, resolve refs, run the function, write outputs.

        Mirrors :func:`pymonik.worker._process` minus the ArmoniK plumbing.
        For multi-output tasks ``output_ids`` carries N ids in stable
        sorted-field order; the worker writes each field's bytes to the
        matching id.
        """
        sess_token = _current_session.set(self)
        spliced_path: str | None = None
        prior_env: dict[str, str | None] | None = None
        primary_output = output_ids[0]

        # Find the future registered for this dispatch's primary output.
        with self._lock:
            fut = self._pending.get(primary_output)
        if fut is None:
            log.warning("local dispatch: no future registered", output_id=primary_output)
            _current_session.reset(sess_token)
            return

        try:
            # Build the data_deps dict by waiting for each upstream result.
            data_deps: dict[str, bytes] = {}
            for rid in data_dep_ids:
                with self._lock:
                    ev = self._result_events.get(rid)
                if ev is not None:
                    ev.wait()
                with self._lock:
                    if rid in self._result_bytes:
                        data_deps[rid] = self._result_bytes[rid]
                    elif rid in self._blob_bytes:
                        data_deps[rid] = self._blob_bytes[rid]
                    else:
                        # Upstream cancelled or missing.
                        fut._resolve_error(
                            TaskFailed(task_id, f"upstream {rid} unavailable")
                        )
                        return

            if cancel_ev.is_set():
                fut._resolve_error(TaskCancelled(task_id))
                return

            # Decode the envelope and resolve refs.
            try:
                envelope = env_mod.decode(payload_bytes)
            except Exception as e:
                fut._resolve_error(
                    TaskFailed(task_id, f"local envelope decode failed: {e!r}")
                )
                return

            # If env_spec.deps + isolate=True, run via subprocess for fidelity
            # with the worker. ``isolate=False`` falls through to the inline
            # path after splicing the venv's site-packages into sys.path.
            if (
                envelope.env_spec is not None
                and envelope.env_spec.deps
                and envelope.env_spec.isolate
            ):
                from pymonik._internal.subprocess_dispatch import run_in_subprocess

                try:
                    pickled = run_in_subprocess(
                        env_spec=envelope.env_spec,
                        envelope_bytes=payload_bytes,
                        data_deps=data_deps,
                    )
                except TaskFailed as e:
                    fut._resolve_error(e)
                    return
                except Exception as e:
                    fut._resolve_error(
                        TaskFailed(task_id, f"local subprocess dispatch failed: {e!r}")
                    )
                    return
                # Subprocess path is single-output (multi-output is rejected
                # upstream in worker._process for isolate=True because the
                # subprocess child has no agent-sidecar channel).
                self._write_result_bytes(output_ids[0], pickled)
                if self._cache is not None and fut._cache_key is not None:
                    try:
                        self._cache.put_bytes(fut._cache_key, pickled)
                    except Exception as e:  # noqa: BLE001
                        log.warning("cache write failed", error=str(e))
                fut._resolve_ok(pickled)
                return

            if envelope.env_spec is not None:
                from pymonik._internal.env_builder import (
                    apply_env_overlay,
                    ensure_env,
                    venv_site_packages,
                )
                import sys as _sys

                try:
                    if envelope.env_spec.deps and not envelope.env_spec.isolate:
                        venv_dir = ensure_env(envelope.env_spec)
                        site = str(venv_site_packages(venv_dir))
                        if site not in _sys.path:
                            _sys.path.insert(0, site)
                            spliced_path = site
                    if envelope.env_spec.env:
                        prior_env = apply_env_overlay(envelope.env_spec.env)
                except Exception as e:
                    fut._resolve_error(
                        TaskFailed(task_id, f"local env build failed: {e!r}")
                    )
                    return

            # Worker-side context (logger, attempt, cancel hook).
            fake_th = _FakeTaskHandler(task_id=task_id, session_id=self._session_id)
            worker_ctx = WorkerContext(
                fake_th,
                attempt=envelope.attempt,
                cancel_check=cancel_ev.is_set,
            )
            ctx_token = ctx_mod._set(worker_ctx)
            try:
                try:
                    from pymonik._internal import _otel as _otel_mod

                    with _otel_mod.use_extracted_context(dict(envelope.otel_context)):
                        with _otel_mod.start_span(
                            "pymonik.task.dispatch",
                            attrs={
                                "pymonik.func": envelope.func_name,
                                "pymonik.task_id": task_id,
                                "pymonik.attempt": envelope.attempt,
                                "pymonik.data_deps": len(data_deps),
                                "pymonik.local": True,
                            },
                            kind="server",
                        ):
                            with _otel_mod.start_span(
                                "pymonik.task.decode",
                                attrs={
                                    "pymonik.fn_pickle_bytes": len(
                                        envelope.function_pickle
                                    ),
                                    "pymonik.args_pickle_bytes": len(
                                        envelope.args_pickle
                                    ),
                                },
                            ):
                                func = cloudpickle.loads(envelope.function_pickle)
                                args, kwargs = cloudpickle.loads(envelope.args_pickle)
                            if data_deps:
                                with _otel_mod.start_span(
                                    "pymonik.task.resolve_refs",
                                    attrs={
                                        "pymonik.data_deps": len(data_deps),
                                        "pymonik.bytes_in": sum(
                                            len(v) for v in data_deps.values()
                                        ),
                                    },
                                ):
                                    args = tuple(
                                        resolve_refs(a, data_deps) for a in args
                                    )
                                    kwargs = {
                                        k: resolve_refs(v, data_deps)
                                        for k, v in kwargs.items()
                                    }
                            else:
                                args = tuple(resolve_refs(a, data_deps) for a in args)
                                kwargs = {
                                    k: resolve_refs(v, data_deps)
                                    for k, v in kwargs.items()
                                }
                            with _otel_mod.start_span(
                                "pymonik.task.run",
                                attrs={
                                    "pymonik.func": envelope.func_name,
                                    "pymonik.task_id": task_id,
                                    "pymonik.attempt": envelope.attempt,
                                    "pymonik.local": True,
                                },
                                kind="server",
                            ):
                                result = func(*args, **kwargs)
                except TaskCancelled:
                    fut._resolve_error(TaskCancelled(task_id))
                    return
                except Exception as e:
                    tb = traceback.format_exc()
                    fut._resolve_error(
                        TaskFailed(task_id, f"{type(e).__name__}: {e}\n{tb}")
                    )
                    return
            finally:
                ctx_mod._reset(ctx_token)

            self._dispatch_result(
                result=result,
                envelope=envelope,
                task_id=task_id,
                output_ids=output_ids,
                fut=fut,
            )

        finally:
            if prior_env is not None:
                from pymonik._internal.env_builder import restore_env_overlay

                restore_env_overlay(prior_env)
            if spliced_path is not None:
                import sys as _sys

                try:
                    _sys.path.remove(spliced_path)
                except ValueError:
                    pass
            with self._lock:
                for oid in output_ids:
                    self._cancel_events.pop(oid, None)
            _current_session.reset(sess_token)


class _LocalBackend:
    """In-process SubmissionBackend.

    ``allocate_outputs`` mints synthetic ids, ``upload_payloads`` parks
    bytes in the session's payload dict, and ``submit`` stashes
    per-task dispatch parameters keyed by output id. The dispatcher is
    fired by :meth:`LocalSession._submit_many`'s ``on_submitted`` hook
    (via :meth:`_launch_for`) — that ordering ensures the future is in
    ``self._pending`` before the dispatcher thread starts looking for it.
    """

    __slots__ = ("_s", "_dispatches")

    def __init__(self, session: LocalSession) -> None:
        self._s = session
        # primary_output_id -> (task_id, payload_bytes, data_dep_ids, all_output_ids)
        self._dispatches: dict[str, tuple[str, bytes, list[str], list[str]]] = {}

    @property
    def session_id(self) -> str:
        return self._s.session_id

    @property
    def allowed_partitions(self) -> tuple[str, ...] | None:
        return None

    def allocate_outputs(self, names: list[str]) -> list[str]:
        ids: list[str] = []
        with self._s._lock:
            for _ in names:
                rid = f"local-out-{uuid.uuid4().hex[:12]}"
                self._s._result_events[rid] = threading.Event()
                ids.append(rid)
        return ids

    def upload_payloads(self, named_data: dict[str, bytes]) -> dict[str, str]:
        out: dict[str, str] = {}
        with self._s._lock:
            for name, data in named_data.items():
                rid = f"local-pl-{uuid.uuid4().hex[:12]}"
                self._s._payloads[rid] = data
                out[name] = rid
        return out

    def submit(
        self,
        definitions: list[TaskDefinition],
        default_options: TaskOptions,
    ) -> list[str]:
        task_ids: list[str] = []
        for d in definitions:
            tid = f"local-task-{uuid.uuid4().hex[:12]}"
            task_ids.append(tid)
            output_ids = list(d.expected_output_ids)
            primary = output_ids[0]
            with self._s._lock:
                payload_bytes = self._s._payloads[d.payload_id]
                self._dispatches[primary] = (
                    tid,
                    payload_bytes,
                    list(d.data_dependencies),
                    output_ids,
                )
        return task_ids

    def _launch_for(self, output_ids: list[str]) -> None:
        """Schedule the dispatch job for the given output id group.

        Called from ``LocalSession._submit_many``'s ``on_submitted`` hook
        after the future(s) are registered. ``output_ids[0]`` is the
        primary key for ``_dispatches``; the dispatcher writes to all of
        ``output_ids``.
        """
        primary = output_ids[0]
        dispatch = self._dispatches.pop(primary, None)
        if dispatch is None:
            return
        task_id, payload_bytes, data_dep_ids, all_output_ids = dispatch
        cancel_ev = threading.Event()
        with self._s._lock:
            for oid in all_output_ids:
                self._s._cancel_events[oid] = cancel_ev
        executor = self._s._cluster._executor
        assert executor is not None, "LocalCluster is not running"
        executor.submit(
            self._s._dispatch,
            task_id,
            all_output_ids,
            payload_bytes,
            data_dep_ids,
            cancel_ev,
        )
