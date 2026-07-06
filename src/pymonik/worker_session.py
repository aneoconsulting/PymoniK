"""WorkerSession — what ``task.spawn(...)`` and ``task.tail(...)`` use from
*inside* a worker task.

Routes through the agent sidecar (``task_handler.create_results_metadata``
/ ``create_results`` / ``submit_tasks``) instead of the control plane,
since workers don't have a control-plane channel.

Two submission paths:

- **Regular spawn** — fresh output result_id; the child produces its own
  result. Use when you want to continue and maybe pass the future to
  another spawn.
- **Tail-call** — the worker dispatcher binds a returned ``TailPromise``
  to one of the parent's expected output ids and submits via
  :meth:`WorkerSession._submit_tail`. The child writes directly to the
  parent's output id, which ArmoniK delivers to whoever was awaiting
  the parent's result.

Intermediate futures created by ``.spawn()`` carry only ``result_id`` /
``task_id`` — the worker has no poller, so ``.result()`` on them raises.
They're useful for passing into further ``.spawn()`` calls (creating
data_dependencies edges inside the DAG).

Submission for ``.spawn()`` goes through
:func:`pymonik._internal.submit.submit_many`. Tail-call submissions are
single, output-id-pinned, and bypass the pipeline's allocation step —
:meth:`_submit_tail` does its own envelope build + create_results +
submit_tasks.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import cloudpickle
from armonik.common import TaskDefinition, TaskOptions

from pymonik import blob as blob_mod
from pymonik import envelope as env_mod
from pymonik._internal._logging import get_logger
from pymonik._internal._otel import current_trace_id_hex, inject_context
from pymonik._internal.refs import auto_spill, extract_deps
from pymonik._internal.submit import submit_many
from pymonik.envelope import EnvSpec, TaskEnvelope
from pymonik.errors import PymonikError
from pymonik.future import Future, FutureList, MultiResultHandle
from pymonik.options import EMPTY

# Same default as the client-side session; see session._DEFAULT_SPILL_THRESHOLD.
_DEFAULT_SPILL_THRESHOLD = 256 * 1024

if TYPE_CHECKING:
    from armonik.worker import TaskHandler
    from pymonik.multiresult import TailPromise
    from pymonik.task import Task

log = get_logger(__name__)


class WorkerSession:
    """Session facade that submits via the agent sidecar.

    Installed as the ``_current_session`` ContextVar for the duration of a
    @task function's execution on a worker.
    """

    __slots__ = ("_th", "_parent_output_ids", "_blob_cache", "_spill_threshold")

    def __init__(
        self,
        task_handler: "TaskHandler",
        *,
        parent_output_ids: list[str],
    ) -> None:
        self._th = task_handler
        self._parent_output_ids = parent_output_ids
        self._blob_cache: dict[str, str] = {}
        self._spill_threshold = _DEFAULT_SPILL_THRESHOLD

    @property
    def session_id(self) -> str:
        return self._th.session_id

    @property
    def parent_output_ids(self) -> list[str]:
        return self._parent_output_ids

    def _upload_blob(self, data: bytes) -> str:
        """Upload via the agent sidecar; dedup within this worker's invocation."""
        h = blob_mod.content_hash(data)
        cached = self._blob_cache.get(h)
        if cached is not None:
            return cached
        name = f"{self.session_id}__blob__{h[:16]}"
        result_map = self._th.create_results(results_data={name: data})
        rid = result_map[name].result_id
        self._blob_cache[h] = rid
        log.info("blob uploaded (worker)", hash=h[:16], size=len(data), result_id=rid)
        return rid

    # ---- submission ----

    def _submit_one(
        self,
        task: "Task[Any, Any]",
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Eager spawn — submits with fresh output id(s).

        Returns ``Future`` for single-output tasks, ``MultiResultHandle``
        for multi-output. Both are worker-stub flavoured (can't be
        awaited; pass to other ``.spawn()``s or ignore).
        """
        return self._submit_many(task, [(args, kwargs)])[0]

    def _submit_many(
        self,
        task: "Task[Any, Any]",
        calls: list[Any],
    ) -> FutureList[Any]:
        """Submit N invocations via the shared pipeline."""
        backend = _AgentBackend(self)
        multi_fields = task.multi_fields

        def make_stub(
            task_id: str,
            output_ids: list[str],
            _args: tuple[Any, ...],
            _kwargs: dict[str, Any],
        ) -> Any:
            if multi_fields:
                field_to_future = {
                    field: Future._new_worker_stub(
                        session=self, task_id=task_id, result_id=oid
                    )
                    for field, oid in zip(multi_fields, output_ids)
                }
                return MultiResultHandle(self, task_id, field_to_future)
            return Future._new_worker_stub(
                session=self, task_id=task_id, result_id=output_ids[0]
            )

        return submit_many(
            task=task,
            calls=calls,
            backend=backend,
            blob_uploader=self._upload_blob,
            spill_threshold=self._spill_threshold,
            default_opts=EMPTY,
            partition="",
            future_factory=make_stub,
            apply_retry_policy=False,
            attempt=1,
        )

    def _submit_tail(
        self,
        promise: "TailPromise[Any]",
        *,
        expected_output_ids: list[str],
    ) -> str:
        """Submit a tail-call promise with caller-supplied output ids.

        Bypasses :func:`submit_many` because the parent already owns the
        output ids. Submits via the agent sidecar exactly the same way
        :class:`_AgentBackend` does, but without going through the
        allocate-outputs step.

        Returns the new task id (mostly for logging — parent doesn't
        await the child).
        """
        task = promise._task
        args = promise._args
        kwargs = promise._kwargs

        deps: list[str] = []
        args_rewritten = tuple(extract_deps(a, deps) for a in args)
        kwargs_rewritten = {k: extract_deps(v, deps) for k, v in kwargs.items()}
        args_rewritten = tuple(
            auto_spill(a, deps, upload_blob=self._upload_blob, threshold=self._spill_threshold)
            for a in args_rewritten
        )
        kwargs_rewritten = {
            k: auto_spill(v, deps, upload_blob=self._upload_blob, threshold=self._spill_threshold)
            for k, v in kwargs_rewritten.items()
        }

        merged_opts = task.opts  # workers don't carry session defaults

        env_dict = merged_opts.env or {}
        env_spec: EnvSpec | None = None
        if merged_opts.deps or env_dict:
            env_spec = EnvSpec(
                deps=tuple(merged_opts.deps or ()),
                isolate=merged_opts.isolate if merged_opts.isolate is not None else False,
                index_url=merged_opts.index_url or "",
                env=tuple(sorted(env_dict.items())),
            )

        traceparent_carrier: dict[str, str] = {}
        inject_context(traceparent_carrier)

        envelope = TaskEnvelope(
            function_pickle=cloudpickle.dumps(task.func),
            args_pickle=cloudpickle.dumps((args_rewritten, kwargs_rewritten)),
            func_name=task.name,
            attempt=1,
            env_spec=env_spec,
            otel_context=tuple(sorted(traceparent_carrier.items())),
            multi_fields=task.multi_fields or (),
        )

        payload_name = f"{self.session_id}__pl__{task.name}__tail__{uuid.uuid4()}"
        result_map = self._th.create_results(
            results_data={payload_name: env_mod.encode(envelope)}
        )
        payload_id = result_map[payload_name].result_id

        per_task_options = merged_opts.to_armonik(default_partition="")
        # Same name stamp as ``submit_many`` so delegated children carry
        # their @task name to the cluster for introspection / the graph.
        per_task_options.options["pymonik.task_name"] = task.name

        definition = TaskDefinition(
            payload_id=payload_id,
            expected_output_ids=expected_output_ids,
            data_dependencies=sorted(set(deps)),
        )

        submitted = self._th.submit_tasks(
            tasks=[definition], default_task_options=per_task_options
        )
        new_task_id = submitted[0].id
        log.info(
            "tail submitted",
            func=task.name,
            child_task=new_task_id,
            expected_outputs=expected_output_ids,
            trace_id=current_trace_id_hex(),
        )
        return new_task_id


class _AgentBackend:
    """SubmissionBackend that routes through the agent sidecar TaskHandler."""

    __slots__ = ("_ws",)

    def __init__(self, ws: WorkerSession) -> None:
        self._ws = ws

    @property
    def session_id(self) -> str:
        return self._ws.session_id

    @property
    def allowed_partitions(self) -> tuple[str, ...] | None:
        return None

    def allocate_outputs(self, names: list[str]) -> list[str]:
        m = self._ws._th.create_results_metadata(result_names=names)
        return [m[n].result_id for n in names]

    def upload_payloads(self, named_data: dict[str, bytes]) -> dict[str, str]:
        m = self._ws._th.create_results(results_data=named_data)
        return {n: r.result_id for n, r in m.items()}

    def submit(
        self,
        definitions: list[TaskDefinition],
        default_options: TaskOptions,
    ) -> list[str]:
        submitted = self._ws._th.submit_tasks(
            tasks=definitions, default_task_options=default_options
        )
        return [s.id for s in submitted]
