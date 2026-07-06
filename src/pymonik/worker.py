"""Worker entrypoint.

Baked into the PymoniK worker image. On task arrival:

1. Decode the msgspec envelope from ``task_handler.payload``.
2. Unpickle the function and ``(args, kwargs)``.
3. Walk args/kwargs, replacing any ``FutureRef`` with the downloaded bytes
   of the corresponding result from ``task_handler.data_dependencies``.
4. Open a ``WorkerContext`` (reachable via ``pymonik.current()``) and a
   worker-side ``WorkerSession`` (so ``task.spawn(...)`` from inside the
   user function submits via the agent sidecar).
5. Call the function.
6. If it returns a ``Future``, treat that as a tail call — the referenced
   task has been submitted with ``expected_output_ids=[our_own_output]``
   and is now ArmoniK's responsibility to deliver. Otherwise, pickle the
   return value and send it as the expected output.

Errors surface as ``Output(error_message)`` so ArmoniK marks the task as
failed and the client raises ``TaskFailed``.
"""

from __future__ import annotations

import contextvars
import traceback
from typing import Any

import cloudpickle
from pymonik._internal._logging import get_logger
from armonik.common import Output
from armonik.worker import TaskHandler, armonik_worker

from pymonik import context as ctx_mod
from pymonik import envelope as env_mod
from pymonik._internal import _otel
from pymonik._internal.refs import resolve_refs
from pymonik.context import WorkerContext
from pymonik.errors import TaskCancelled
from pymonik.future import Future, MultiResultHandle
from pymonik.worker_session import WorkerSession

log = get_logger(__name__)


# Populated by the patched ``ArmoniKWorker.Process`` (see :func:`_patch_process`),
# read inside :func:`_process` to give WorkerContext a handle on the gRPC
# server context so user code can observe cancellation.
_grpc_ctx_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "_pymonik_grpc_ctx", default=None
)


def _patch_process() -> None:
    """Wrap ``ArmoniKWorker.Process`` so the gRPC context reaches our dispatcher.

    Upstream ``armonik.worker.ArmoniKWorker.Process`` accepts (request,
    context) from the gRPC server but only passes ``request`` through to
    our processor (via ``TaskHandler``). We need ``context.is_active()``
    for cancellation; this wrapper stashes the context in a
    :class:`contextvars.ContextVar` for the duration of the call. Idempotent.
    """
    from armonik.worker.worker import ArmoniKWorker

    if getattr(ArmoniKWorker.Process, "_pymonik_patched", False):
        return
    _original = ArmoniKWorker.Process

    def _patched(self, request, context):
        token = _grpc_ctx_var.set(context)
        try:
            return _original(self, request, context)
        finally:
            _grpc_ctx_var.reset(token)

    _patched._pymonik_patched = True  # type: ignore[attr-defined]
    ArmoniKWorker.Process = _patched  # type: ignore[method-assign]


def _dispatch_result(
    result: Any,
    *,
    envelope: env_mod.TaskEnvelope,
    task_handler: TaskHandler,
    parent_output_ids: list[str],
    session: "WorkerSession",
) -> Output:
    """Map a user-function return value onto ArmoniK output writes / submits.

    Three return shapes are valid:

    - ``TailPromise`` — whole-task tail-call. Submit the child with the
      parent's full set of expected output ids. Parent task returns
      ``Output()`` (the child writes everything).
    - ``MultiResult`` — multi-output. Each field is either a plain value
      (cloudpickled and written directly) or a ``TailPromise`` (per-field
      delegation: submit a child task with that field's output id).
    - Anything else — single-output, write the cloudpickled value to
      ``parent_output_ids[0]``.

    Returns the ``Output`` to hand back to the agent.
    """
    from pymonik.multiresult import MultiResult, TailPromise

    # Re-attach the propagated trace context so any span we open here
    # (notably ``pymonik.task.send_results``) chains under
    # ``pymonik.submit`` instead of becoming a new trace root.
    with _otel.use_extracted_context(dict(envelope.otel_context)):
        return _dispatch_result_inner(
            result,
            envelope=envelope,
            task_handler=task_handler,
            parent_output_ids=parent_output_ids,
            session=session,
        )


def _dispatch_result_inner(
    result: Any,
    *,
    envelope: env_mod.TaskEnvelope,
    task_handler: TaskHandler,
    parent_output_ids: list[str],
    session: "WorkerSession",
) -> Output:
    from pymonik.multiresult import MultiResult, TailPromise

    multi_fields: tuple[str, ...] = envelope.multi_fields

    # ---- whole-task tail-call ----
    if isinstance(result, TailPromise):
        child_task = result._task
        child_multi: tuple[str, ...] = child_task.multi_fields or ()
        if multi_fields:
            # Parent declares N outputs; child must match the schema.
            if child_multi != multi_fields:
                return Output(
                    f"worker error: tail-called task {child_task.name!r} declares "
                    f"fields {list(child_multi)} but parent declares "
                    f"{list(multi_fields)} — shapes must match for whole-task "
                    f"tail-call."
                )
            session._submit_tail(result, expected_output_ids=parent_output_ids)
        else:
            # Parent is single-output; child must be too.
            if child_multi:
                return Output(
                    f"worker error: tail-called task {child_task.name!r} is "
                    f"multi-output ({list(child_multi)}) but parent is "
                    f"single-output. Wrap the call in a multi-output parent "
                    f"or pick a single-output child."
                )
            session._submit_tail(result, expected_output_ids=parent_output_ids)
        log.info(
            "task tail-called",
            task_id=task_handler.task_id,
            child_func=child_task.name,
        )
        return Output()

    # ---- multi-output return ----
    if isinstance(result, MultiResult):
        if not multi_fields:
            return Output(
                "worker error: function returned MultiResult but task wasn't "
                "declared multi-output (decoration didn't extract a field "
                "schema). Construct MultiResult with literal kwargs in the "
                "task body, or pass outputs=(...) to the @task decorator."
            )
        returned = set(result.fields.keys())
        declared = set(multi_fields)
        if returned != declared:
            missing = declared - returned
            extra = returned - declared
            details = []
            if missing:
                details.append(f"missing {sorted(missing)}")
            if extra:
                details.append(f"extra {sorted(extra)}")
            return Output(
                f"worker error: MultiResult shape mismatch ({', '.join(details)}). "
                f"Declared: {sorted(declared)}; returned: {sorted(returned)}."
            )

        field_to_oid = dict(zip(multi_fields, parent_output_ids))
        pending_writes: dict[str, bytes] = {}

        for field, value in result.fields.items():
            oid = field_to_oid[field]
            if isinstance(value, TailPromise):
                # Per-field delegation. The promise's task must be
                # single-output (no nested multi-result).
                if value._task.multi_fields:
                    return Output(
                        f"worker error: field {field!r} delegates to "
                        f"{value._task.name!r} which is multi-output. "
                        f"Per-field tail-call requires a single-output task; "
                        f"forward via a passthrough task instead."
                    )
                session._submit_tail(value, expected_output_ids=[oid])
            elif isinstance(value, Future):
                return Output(
                    f"worker error: field {field!r} is a Future from .spawn(). "
                    f"To delegate this field, use .tail() instead — "
                    f"MultiResult({field}=other.tail(args), ...)."
                )
            elif isinstance(value, MultiResultHandle):
                return Output(
                    f"worker error: field {field!r} is a MultiResultHandle. "
                    f"Per-field nested multi-output access isn't supported; "
                    f"insert a passthrough single-output task to forward "
                    f"the specific field."
                )
            else:
                pending_writes[oid] = cloudpickle.dumps(value)

        if pending_writes:
            with _otel.start_span(
                "pymonik.task.send_results",
                attrs={
                    "pymonik.outputs": len(pending_writes),
                    "pymonik.bytes_out": sum(len(v) for v in pending_writes.values()),
                },
            ):
                task_handler.send_results(pending_writes)
        log.info(
            "task completed (multi)",
            task_id=task_handler.task_id,
            func=envelope.func_name,
            fields=list(multi_fields),
        )
        return Output()

    # ---- plain single-output return ----
    if multi_fields:
        return Output(
            f"worker error: task declared multi-output fields {list(multi_fields)} "
            f"but returned a {type(result).__name__} (expected MultiResult)."
        )
    pickled = cloudpickle.dumps(result)
    with _otel.start_span(
        "pymonik.task.send_results",
        attrs={
            "pymonik.outputs": 1,
            "pymonik.bytes_out": len(pickled),
        },
    ):
        task_handler.send_results({parent_output_ids[0]: pickled})
    log.info("task completed", task_id=task_handler.task_id, func=envelope.func_name)
    return Output()


def _process(task_handler: TaskHandler) -> Output:
    try:
        envelope = env_mod.decode(task_handler.payload)
        log.info(
            "task received",
            task_id=task_handler.task_id,
            session_id=task_handler.session_id,
            func=envelope.func_name,
            envelope_version=envelope.version,
            data_deps=len(task_handler.data_dependencies or {}),
            deps=list(envelope.env_spec.deps) if envelope.env_spec else None,
            isolate=envelope.env_spec.isolate if envelope.env_spec else None,
            env_keys=[k for k, _ in envelope.env_spec.env] if envelope.env_spec else None,
        )

        if not task_handler.expected_results:
            return Output("worker error: no expected_results on the task")
        parent_output_ids = list(task_handler.expected_results)
        is_multi = bool(envelope.multi_fields)

        if is_multi and len(parent_output_ids) != len(envelope.multi_fields):
            return Output(
                f"worker error: envelope declares {len(envelope.multi_fields)} "
                f"output fields but task has {len(parent_output_ids)} "
                f"expected_output_ids"
            )

        data_deps: dict[str, bytes] = dict(task_handler.data_dependencies or {})

        # Subprocess path: deps declared AND isolation requested. The child
        # runs the full pipeline against the env's interpreter; env vars
        # are applied to the child's environment by run_in_subprocess.
        # Note: subprocess path doesn't (yet) support TailPromise / MultiResult
        # — those need agent-sidecar access, which the child doesn't have.
        if envelope.env_spec is not None and envelope.env_spec.deps and envelope.env_spec.isolate:
            from pymonik._internal.subprocess_dispatch import run_in_subprocess

            if is_multi:
                return Output(
                    "worker error: multi-output tasks aren't supported "
                    "with isolate=True (subprocess can't access the agent "
                    "sidecar). Use isolate=False or move to a baked image."
                )
            result_pickle = run_in_subprocess(
                env_spec=envelope.env_spec,
                envelope_bytes=task_handler.payload,
                data_deps=data_deps,
                task_id=task_handler.task_id,
                session_id=task_handler.session_id,
            )
            task_handler.send_results({parent_output_ids[0]: result_pickle})
            log.info(
                "task completed (subprocess)",
                task_id=task_handler.task_id,
                func=envelope.func_name,
            )
            return Output()

        # All other paths run inline in the worker process; they may need
        # to splice a venv into sys.path (deps + !isolate) and/or overlay
        # env vars. Compute the overlay once, restore in finally.
        import sys as _sys
        from pymonik._internal.env_builder import (
            apply_env_overlay,
            ensure_env,
            restore_env_overlay,
            venv_site_packages,
        )

        spliced_path: str | None = None
        prior_env: dict[str, str | None] | None = None
        if envelope.env_spec is not None:
            if envelope.env_spec.deps:
                venv_dir = ensure_env(envelope.env_spec)
                site = str(venv_site_packages(venv_dir))
                if site not in _sys.path:
                    _sys.path.insert(0, site)
                    spliced_path = site
            if envelope.env_spec.env:
                prior_env = apply_env_overlay(envelope.env_spec.env)

        try:
            # Re-attach the trace context the client injected so all the
            # phase spans below become children of pymonik.submit, then
            # open one outer ``pymonik.task.dispatch`` span that covers
            # every phase (decode → resolve → run → send) so the user
            # can see where worker wall-time actually goes. Each phase
            # is its own child for fine-grained timing.
            otel_carrier = dict(envelope.otel_context)
            with _otel.use_extracted_context(otel_carrier):
                with _otel.start_span(
                    "pymonik.task.dispatch",
                    attrs={
                        "pymonik.func": envelope.func_name,
                        "pymonik.task_id": task_handler.task_id,
                        "pymonik.attempt": envelope.attempt,
                        "pymonik.data_deps": len(data_deps),
                    },
                    kind="server",
                ):
                    with _otel.start_span(
                        "pymonik.task.decode",
                        attrs={
                            "pymonik.fn_pickle_bytes": len(envelope.function_pickle),
                            "pymonik.args_pickle_bytes": len(envelope.args_pickle),
                        },
                    ):
                        func = cloudpickle.loads(envelope.function_pickle)
                        args, kwargs = cloudpickle.loads(envelope.args_pickle)

                    if data_deps:
                        with _otel.start_span(
                            "pymonik.task.resolve_refs",
                            attrs={
                                "pymonik.data_deps": len(data_deps),
                                "pymonik.bytes_in": sum(len(v) for v in data_deps.values()),
                            },
                        ):
                            args = tuple(resolve_refs(a, data_deps) for a in args)
                            kwargs = {k: resolve_refs(v, data_deps) for k, v in kwargs.items()}

                    worker_ctx = WorkerContext(
                        task_handler,
                        grpc_context=_grpc_ctx_var.get(),
                        attempt=envelope.attempt,
                    )
                    # Typed ctx injection: a parameter annotated
                    # ``pymonik.Ctx`` receives the live context as a keyword.
                    if envelope.ctx_param:
                        kwargs[envelope.ctx_param] = worker_ctx
                    session = WorkerSession(task_handler, parent_output_ids=parent_output_ids)

                    from pymonik.task import _current_session as _cs

                    ctx_token = ctx_mod._set(worker_ctx)
                    sess_token = _cs.set(session)
                    try:
                        with _otel.start_span(
                            "pymonik.task.run",
                            attrs={
                                "pymonik.func": envelope.func_name,
                                "pymonik.task_id": task_handler.task_id,
                                "pymonik.attempt": envelope.attempt,
                            },
                            kind="server",
                        ):
                            result: Any = func(*args, **kwargs)
                    finally:
                        _cs.reset(sess_token)
                        ctx_mod._reset(ctx_token)
        finally:
            if prior_env is not None:
                restore_env_overlay(prior_env)
            if spliced_path is not None:
                try:
                    _sys.path.remove(spliced_path)
                except ValueError:
                    pass

        return _dispatch_result(
            result,
            envelope=envelope,
            task_handler=task_handler,
            parent_output_ids=parent_output_ids,
            session=session,
        )

    except TaskCancelled as e:
        # Cooperative cancellation via ``pymonik.current().cancel_if_requested()``.
        # The cluster already has the task marked CANCELLING; our return is
        # mostly cosmetic (the agent's gRPC call is likely already dead).
        log.info("task cancelled cooperatively", task_id=task_handler.task_id)
        return Output(f"cancelled: {e}")

    except Exception as e:
        tb = traceback.format_exc()
        log.error("task failed", task_id=task_handler.task_id, error=str(e))
        return Output(f"{type(e).__name__}: {e}\n{tb}")


def run() -> None:
    """Bound to the ``pymonik-worker`` console script in pyproject.toml.

    Workers always log — operators rely on the polling-agent → k8s
    pipeline to surface what each pod is doing. The library default
    (silent) doesn't fit a long-running worker, so we explicitly call
    :func:`pymonik.enable_logging` here. Override the level via
    ``PYMONIK_WORKER_LOG_LEVEL`` env var.

    Worker logs ship as JSON (one record per line) so the polling
    agent → k8s → Seq pipeline picks up structured fields instead of
    a single opaque message string. Override the level via
    ``PYMONIK_WORKER_LOG_LEVEL``.

    OTel: same auto-detect rule as on the client (env vars present →
    enabled). Workers typically inherit ``OTEL_EXPORTER_OTLP_ENDPOINT``
    from their pod env so they export to the same collector as the
    client.
    """
    import os

    from pymonik._internal._logging import enable_logging

    enable_logging(
        level=os.getenv("PYMONIK_WORKER_LOG_LEVEL", "INFO"),
        json=True,
    )
    _otel.setup(service_name=os.getenv("OTEL_SERVICE_NAME", "pymonik-worker"))
    _patch_process()

    @armonik_worker()
    def processor(task_handler: TaskHandler) -> Output:
        return _process(task_handler)

    processor.run()


if __name__ == "__main__":
    run()
