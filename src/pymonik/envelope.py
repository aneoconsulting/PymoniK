"""Wire envelope.

One msgspec.Struct carrying two cloudpickle blobs — the function and a
``(args, kwargs)`` tuple — plus typed metadata (env spec, OTel context,
multi-output field names, retry attempt, client Python version). Args
that came in as Future / Blob / Materialize have already been replaced
with ``FutureRef`` / ``BlobRef`` / ``MaterializeRef`` sentinels (see
``_internal/refs.py``); the worker re-walks the unpickled tree and
swaps them for the corresponding ``data_dependencies`` bytes.

``version`` lets older workers reject envelopes from a newer client
loudly instead of silently mis-decoding.
"""

from __future__ import annotations

import sys

import msgspec


ENVELOPE_VERSION = 1


def _current_python() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}"


class EnvSpec(msgspec.Struct, frozen=True, kw_only=True):
    """Runtime Python environment requested for a task.

    Empty ``deps`` means "no extras" — the worker runs the task in
    its own process. Non-empty ``deps`` means the worker creates (or
    reuses) a venv at ``/cache/internal/envs/<env_id>`` and dispatches
    the task with that venv on ``sys.path``.

    Default mode is **in-process splice** (``isolate=False``): we add
    the venv's site-packages to the worker's ``sys.path`` and call the
    function inline. ~1 ms per task once warm, but module imports
    persist across tasks on the same pod (they share the worker's
    interpreter), so concurrent sessions with *conflicting* deps lists
    will collide. Opt in to ``isolate=True`` to spawn a fresh Python
    per task — ~400-500 ms each, with full isolation.

    The wire footprint is the deps list itself — strings — never
    a lockfile.
    """

    deps: tuple[str, ...] = ()
    isolate: bool = False
    # Optional private index URL for the worker-side ``uv pip install``.
    # ``""`` means PyPI default.
    index_url: str = ""
    # Environment variables applied to the task. Tuple-of-tuples (sorted)
    # rather than dict so msgspec can hash a frozen Struct, and so the
    # env_id hash is stable.
    env: tuple[tuple[str, str], ...] = ()


class TaskEnvelope(msgspec.Struct, frozen=True, kw_only=True):
    """The payload bytes that travel from client to worker.

    Args:
        version: Schema version. Workers reject envelopes whose version they
            don't recognise.
        python: ``major.minor`` version of the client's interpreter.
            cloudpickle bytecode is not cross-minor-compatible, so the worker
            raises a clear error instead of SIGSEGV'ing mid-unpickle when the
            versions differ.
        function_pickle: cloudpickle bytes of the user function.
        args_pickle: cloudpickle bytes of a ``(args, kwargs)`` tuple.
        func_name: Best-effort human-readable name of the function; surfaced in
            logs on both sides. Non-authoritative — the function is identified
            by its pickle bytes, not by name.
        attempt: 1 for the original submission, 2+ for client-side retries.
        env_spec: optional runtime environment. ``None`` (the default) means
            the worker runs the task with its existing site-packages.
    """

    version: int = ENVELOPE_VERSION
    python: str = msgspec.field(default_factory=_current_python)
    function_pickle: bytes
    args_pickle: bytes
    func_name: str = "<anonymous>"
    attempt: int = 1
    env_spec: "EnvSpec | None" = None
    # W3C trace context propagated from the client (``traceparent``,
    # ``tracestate``). Empty when OTel tracing is disabled. Workers
    # extract before calling the user function so its spans nest under
    # the submitter's.
    otel_context: tuple[tuple[str, str], ...] = ()
    # Sorted field names for multi-output tasks. Empty for single-output
    # tasks. The worker zips ``multi_fields`` against the task handler's
    # ``expected_results`` to map each MultiResult field to its
    # ArmoniK output id.
    multi_fields: tuple[str, ...] = ()
    # Name of the parameter annotated ``pymonik.Ctx`` / ``WorkerContext``,
    # detected at decoration. Empty when the function takes no context
    # parameter. The worker injects the live ``WorkerContext`` under this
    # keyword before calling the function.
    ctx_param: str = ""


def encode(envelope: TaskEnvelope) -> bytes:
    return msgspec.msgpack.encode(envelope)


def decode(data: bytes) -> TaskEnvelope:
    env = msgspec.msgpack.decode(data, type=TaskEnvelope)
    if env.version != ENVELOPE_VERSION:
        raise ValueError(
            f"incompatible envelope version: got {env.version}, "
            f"this worker speaks v{ENVELOPE_VERSION}"
        )
    worker_py = _current_python()
    if env.python and env.python != worker_py:
        raise ValueError(
            f"python version mismatch: client sent cloudpickle bytecode from "
            f"Python {env.python}, this worker runs Python {worker_py}. "
            f"cloudpickle is not cross-minor-compatible; rebuild the worker "
            f"image on Python {env.python} or switch the client to Python {worker_py}."
        )
    return env
