"""Shared submission pipeline.

Three concrete sessions submit tasks for execution: the client-side
:class:`Session` (talks to ArmoniK's control plane), the worker-side
:class:`WorkerSession` (talks to the agent sidecar from inside a running
task), and the in-process :class:`LocalSession` (runs everything in a
thread pool). All three share the same logical pipeline:

    normalise calls
    → extract refs (Future / Blob / Materialize) into the wire envelope
    → auto-spill oversize args
    → cloudpickle (function, args, kwargs)
    → encode TaskEnvelope (msgspec)
    → allocate output result_ids
    → upload payloads
    → submit task definitions
    → wrap each (task_id, output_id) in a Future + apply retry policy

The transport-specific bits — *how* you allocate, upload, and submit —
live behind :class:`SubmissionBackend`. The session-specific bits — what
flavour of Future to build, whether retries apply, whether to register
the future in a pending dict — are passed as small callables to
:func:`submit_many`. Every session does its work via the same
orchestrator; new pipeline features (e.g. ``import_data`` dedup, OTel
attachment) land in one place instead of three.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Protocol

import cloudpickle
from pymonik._internal._logging import get_logger
from armonik.common import TaskDefinition, TaskOptions

from pymonik import envelope as env_mod
from pymonik._internal import _otel
from pymonik._internal.refs import auto_spill, extract_deps
from pymonik.envelope import EnvSpec, TaskEnvelope
from pymonik.errors import PymonikError
from pymonik.future import Future, FutureList
from pymonik.options import TaskOpts, resolve_backoff

log = get_logger(__name__)

if TYPE_CHECKING:
    from pymonik.task import Task


class SubmissionBackend(Protocol):
    """Transport interface used by :func:`submit_many`.

    Three methods, plus a ``session_id`` so payload / output names get a
    namespace prefix. The whole protocol is intentionally tiny so each
    backend (control-plane gRPC, agent sidecar, in-process) can implement
    it without inheriting infrastructure it doesn't need.
    """

    @property
    def session_id(self) -> str: ...

    @property
    def allowed_partitions(self) -> tuple[str, ...] | None:
        """Partitions this backend's session can route into, or ``None``
        for no restriction (worker / local backends, where ArmoniK isn't
        in the loop).
        """
        ...

    def allocate_outputs(self, names: list[str]) -> list[str]:
        """Reserve N output result_ids, one per task to submit."""
        ...

    def upload_payloads(self, named_data: dict[str, bytes]) -> dict[str, str]:
        """Upload payload bytes. Returns ``{requested_name: result_id}``."""
        ...

    def submit(
        self,
        definitions: list[TaskDefinition],
        default_options: TaskOptions,
    ) -> list[str]:
        """Submit N task definitions. Returns ``task_id``s in order."""
        ...


def normalise_calls(
    calls: Iterable[Any],
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Coerce ``spawn`` / ``map`` arg shapes into ``(args, kwargs)`` pairs.

    Accepts:

    - ``[(args_tuple, kwargs_dict), ...]`` — the canonical form used by
      ``_submit_one``.
    - ``[args_tuple, ...]`` — what ``Task.starmap([(1, 2), (3, 4)])`` produces.
    - ``[scalar, ...]`` — single-arg shorthand for one-positional tasks.
    """
    out: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for call in calls:
        if (
            isinstance(call, tuple)
            and len(call) == 2
            and isinstance(call[0], tuple)
            and isinstance(call[1], dict)
        ):
            out.append(call)
        else:
            args_t = call if isinstance(call, tuple) else (call,)
            out.append((args_t, {}))
    return out


def submit_many(
    *,
    task: "Task[Any, Any]",
    calls: Iterable[Any],
    backend: SubmissionBackend,
    blob_uploader: Callable[[bytes], str],
    spill_threshold: int,
    default_opts: TaskOpts,
    partition: str,
    future_factory: Callable[
        [str, list[str], tuple[Any, ...], dict[str, Any]], Any
    ],
    on_submitted: Optional[Callable[[list[str], Any], None]] = None,
    apply_retry_policy: bool = True,
    existing_future: Optional[Future[Any]] = None,
    attempt: int = 1,
) -> FutureList:
    """Run the full submission pipeline.

    For single-output tasks, each task gets one ArmoniK ``expected_output_id``.
    For multi-output tasks (``task.multi_fields`` is set), each task gets
    N output ids, one per declared field, in sorted-field order. The
    ``future_factory`` receives the full list per task and builds either
    a :class:`Future` or a :class:`MultiResultHandle` accordingly.

    Args:
        task: the decorated function (provides ``func`` + ``opts``).
        calls: iterable of call shapes (see :func:`normalise_calls`).
        backend: transport plug-in.
        blob_uploader: callable used by auto-spill to upload oversize args.
        spill_threshold: cloudpickle-size threshold above which inline args
            are auto-spilled to a Blob and replaced with a ``BlobRef``.
        default_opts: session default options (merged with @task opts).
        partition: session partition (backstops ``opts.partition``).
        future_factory: builds a future-shape from ``(task_id, output_ids,
            args, kwargs)``. Returns ``Future`` for single-output, or
            ``MultiResultHandle`` for multi-output.
        on_submitted: optional ``(output_ids, future_or_handle)``
            callback. The session's pending-future dict registers each
            output id keyed to the same future-or-handle so the
            completion loop can resolve any field.
        apply_retry_policy: when True (default), attaches retry state to
            each future per ``task.opts.retry_on/retry_backoff``. Worker
            sessions disable this — workers don't retry.
        existing_future: retry path. Reuse this Future (rewriting its
            ``_task_id``/``_result_id``) instead of constructing a new
            one. Single-task, single-output only.
        attempt: envelope ``attempt`` field. 1 for fresh submissions, ≥2
            for retries.
    """
    normalised = normalise_calls(calls)
    n = len(normalised)
    if n == 0:
        return FutureList([])

    if existing_future is not None and n != 1:
        raise PymonikError("existing_future is only valid for a single submission")

    # Validate partition selection BEFORE we hit the network — no point
    # allocating ids for a request that's about to fail.
    merged_opts = default_opts.merge(task.opts)
    allowed = backend.allowed_partitions
    if allowed is not None and merged_opts.partition is not None:
        if merged_opts.partition not in allowed:
            raise PymonikError(
                f"task {task.name!r} requested partition "
                f"{merged_opts.partition!r}, but the session is only bound "
                f"to {list(allowed)}. Pass that partition to "
                f"client.session(partition=[...]) to enable it."
            )

    multi_fields: tuple[str, ...] = task.multi_fields or ()
    n_outputs_per_task = len(multi_fields) if multi_fields else 1

    if existing_future is not None and n_outputs_per_task != 1:
        raise PymonikError(
            "retry of a multi-output task is not yet supported"
        )

    _otel.setup()

    with _otel.start_span(
        "pymonik.submit",
        attrs={
            "pymonik.func": task.name,
            "pymonik.count": n,
            "pymonik.partition": merged_opts.partition or partition,
            "pymonik.attempt": attempt,
            "pymonik.outputs_per_task": n_outputs_per_task,
        },
        kind="client",
    ) as submit_span:
        traceparent_carrier: dict[str, str] = {}
        _otel.inject_context(traceparent_carrier)
        otel_ctx_tuple: tuple[tuple[str, str], ...] = tuple(
            sorted(traceparent_carrier.items())
        )

        # 1. Allocate output result_ids. For multi-output tasks each
        # task gets N ids, in stable (sorted-field) order.
        output_names: list[str] = []
        for _ in range(n):
            if multi_fields:
                for field in multi_fields:
                    output_names.append(
                        f"{backend.session_id}__out__{task.name}__{field}__{uuid.uuid4()}"
                    )
            else:
                output_names.append(
                    f"{backend.session_id}__out__{task.name}__{uuid.uuid4()}"
                )
        all_output_ids = backend.allocate_outputs(output_names)
        output_groups: list[list[str]] = [
            all_output_ids[i * n_outputs_per_task : (i + 1) * n_outputs_per_task]
            for i in range(n)
        ]

        # 2. Build envelopes per call, collect data deps.
        fn_pickle = cloudpickle.dumps(task.func)
        payload_blobs: dict[str, bytes] = {}
        payload_names: list[str] = []
        task_deps: list[list[str]] = []

        env_dict = merged_opts.env or {}
        env_spec: EnvSpec | None = None
        if merged_opts.deps or env_dict:
            env_spec = EnvSpec(
                deps=tuple(merged_opts.deps or ()),
                isolate=merged_opts.isolate if merged_opts.isolate is not None else False,
                index_url=merged_opts.index_url or "",
                env=tuple(sorted(env_dict.items())),
            )

        for args, kwargs in normalised:
            deps: list[str] = []
            args_rewritten = tuple(extract_deps(a, deps) for a in args)
            kwargs_rewritten = {k: extract_deps(v, deps) for k, v in kwargs.items()}
            args_rewritten = tuple(
                auto_spill(a, deps, upload_blob=blob_uploader, threshold=spill_threshold)
                for a in args_rewritten
            )
            kwargs_rewritten = {
                k: auto_spill(v, deps, upload_blob=blob_uploader, threshold=spill_threshold)
                for k, v in kwargs_rewritten.items()
            }
            envelope = TaskEnvelope(
                function_pickle=fn_pickle,
                args_pickle=cloudpickle.dumps((args_rewritten, kwargs_rewritten)),
                func_name=task.name,
                attempt=attempt,
                env_spec=env_spec,
                otel_context=otel_ctx_tuple,
                multi_fields=multi_fields,
            )
            name = f"{backend.session_id}__pl__{task.name}__{uuid.uuid4()}"
            payload_names.append(name)
            payload_blobs[name] = env_mod.encode(envelope)
            task_deps.append(sorted(set(deps)))

        # 3. Upload payloads.
        payload_id_map = backend.upload_payloads(payload_blobs)
        payload_ids = [payload_id_map[name] for name in payload_names]

        # 4. Per-task options uniform per batch — see comment in
        # _ClientBackend on why options= isn't on each TaskDefinition.
        per_task_options = merged_opts.to_armonik(default_partition=partition)

        # 5. Submit.
        definitions = [
            TaskDefinition(
                payload_id=pid,
                expected_output_ids=oids,
                data_dependencies=deps,
            )
            for pid, oids, deps in zip(payload_ids, output_groups, task_deps)
        ]
        task_ids = backend.submit(definitions, per_task_options)

        # 6. Build / rewire futures, attach retry policy, register.
        retry_policy: tuple[int, tuple[type[BaseException], ...], Any] | None = None
        if apply_retry_policy and task.opts.retry_on:
            backoff_fn = resolve_backoff(task.opts.retry_backoff)
            max_retries = task.opts.retries if task.opts.retries is not None else 3
            retry_policy = (max_retries, tuple(task.opts.retry_on), backoff_fn)

        futures: list[Any] = []
        for (args, kwargs), task_id, output_ids in zip(
            normalised, task_ids, output_groups
        ):
            if existing_future is not None:
                fut = existing_future
                fut._task_id = task_id
                fut._result_id = output_ids[0]
            else:
                fut = future_factory(task_id, output_ids, args, kwargs)
            if retry_policy is not None:
                # Retries only fire for single-output tasks (the
                # ``_retry_state`` slot lives on Future, not
                # MultiResultHandle).
                max_r, on_types, backoff_fn = retry_policy
                if hasattr(fut, "_retry_state"):
                    fut._retry_state = (task, args, kwargs, max_r, on_types, backoff_fn)
            if on_submitted is not None:
                on_submitted(output_ids, fut)
            futures.append(fut)

        if submit_span is not None and futures:
            first_task_id = getattr(futures[0], "task_id", None)
            if first_task_id is not None:
                submit_span.set_attribute("pymonik.first_task_id", first_task_id)

        log.info(
            "batch submitted",
            func=task.name,
            count=n,
            first_task=getattr(futures[0], "task_id", None) if futures else None,
            any_deps=any(task_deps),
            attempt=attempt,
            outputs_per_task=n_outputs_per_task,
            trace_id=_otel.current_trace_id_hex(),
        )
        return FutureList(futures)
