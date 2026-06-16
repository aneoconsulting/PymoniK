"""Subprocess entry point for ``deps``-isolated task execution.

Invoked as ``python -m pymonik._internal.task_runner`` by the worker
when ``env_spec.deps`` is non-empty and ``env_spec.isolate`` is True.

Wire protocol on stdin (binary, length-prefixed):

    [4 bytes BE u32] envelope length
    [N bytes]        msgspec-encoded TaskEnvelope
    [4 bytes BE u32] data_deps map length (number of entries)
    repeated N times:
      [4 bytes BE u32] key length, then UTF-8 key
      [4 bytes BE u32] value length, then value bytes

Wire protocol on stdout (binary):

    [1 byte] tag: b'r' for result, b'e' for error
    [4 bytes BE u32] payload length
    [N bytes]        cloudpickled return value (tag=r)
                     OR utf-8 error message (tag=e)

Stderr is for diagnostics (uv install logs, user prints, traceback on
unexpected crash). Mixing stdout for the result and stderr for diagnostics
keeps user ``print()`` calls from corrupting the wire — the parent only
reads stdout for the framed result.

This module deliberately has minimal imports at module-load time —
it runs inside the per-deps venv, where pymonik *is* installed (parent
spawns with ``PYTHONPATH`` set to the worker's site-packages so we can
import ``pymonik`` without re-installing it into every venv).
"""

from __future__ import annotations

import struct
import sys
import traceback
from typing import Any


def _read_exact(stream, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        b = stream.read(remaining)
        if not b:
            raise EOFError(f"task_runner: stdin closed with {remaining} bytes pending")
        chunks.append(b)
        remaining -= len(b)
    return b"".join(chunks)


def _read_u32(stream) -> int:
    return struct.unpack(">I", _read_exact(stream, 4))[0]


def _read_input(stream) -> tuple[bytes, dict[str, bytes]]:
    env_len = _read_u32(stream)
    envelope_bytes = _read_exact(stream, env_len)
    n_deps = _read_u32(stream)
    deps: dict[str, bytes] = {}
    for _ in range(n_deps):
        klen = _read_u32(stream)
        key = _read_exact(stream, klen).decode("utf-8")
        vlen = _read_u32(stream)
        deps[key] = _read_exact(stream, vlen)
    return envelope_bytes, deps


def _write_result(stream, tag: bytes, payload: bytes) -> None:
    stream.write(tag)
    stream.write(struct.pack(">I", len(payload)))
    stream.write(payload)
    stream.flush()


class _DetachedHandler:
    """Stand-in task handler for the detached child process.

    Carries only the identity the parent forwards via env vars. It has no
    agent-sidecar channel and no gRPC server context, so the
    ``WorkerContext`` built from it can surface ``task_id`` / ``session_id``
    / ``attempt`` / ``log`` but not cancellation or result-sending.
    """

    __slots__ = ("task_id", "session_id")

    def __init__(self, task_id: str, session_id: str) -> None:
        self.task_id = task_id
        self.session_id = session_id


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    try:
        envelope_bytes, data_deps = _read_input(stdin)
    except Exception as e:
        msg = f"task_runner: failed to read input: {e}\n{traceback.format_exc()}"
        try:
            _write_result(stdout, b"e", msg.encode("utf-8", errors="replace"))
        except Exception:
            sys.stderr.write(msg)
        return 1

    try:
        import os

        import cloudpickle

        from pymonik import envelope as env_mod
        from pymonik._internal import _otel
        from pymonik._internal.refs import resolve_refs

        # OTel: same env-driven auto-enable as the parent worker. Spans
        # from the subprocess become children of pymonik.submit through
        # the propagated trace context in the envelope.
        _otel.setup(service_name=os.getenv("OTEL_SERVICE_NAME", "pymonik-worker"))

        from pymonik import context as _ctx_mod
        from pymonik.context import WorkerContext

        env = env_mod.decode(envelope_bytes)
        func = cloudpickle.loads(env.function_pickle)
        args, kwargs = cloudpickle.loads(env.args_pickle)
        args = tuple(resolve_refs(a, data_deps) for a in args)
        kwargs = {k: resolve_refs(v, data_deps) for k, v in kwargs.items()}

        # Worker context for this detached child: both pymonik.current()
        # and an injected ``ctx: pymonik.Ctx`` parameter work here. Identity
        # comes from env vars the parent set (see subprocess_dispatch). The
        # child can't observe cancellation or reach the sidecar, so that
        # surface of the context is inert — a deliberate isolate-mode limit.
        worker_ctx = WorkerContext(
            _DetachedHandler(
                os.getenv("PYMONIK_TASK_ID", ""),
                os.getenv("PYMONIK_SESSION_ID", ""),
            ),
            attempt=env.attempt,
        )
        _ctx_mod._set(worker_ctx)
        if env.ctx_param:
            kwargs[env.ctx_param] = worker_ctx

        with _otel.use_extracted_context(dict(env.otel_context)):
            with _otel.start_span(
                "pymonik.task.run",
                attrs={
                    "pymonik.func": env.func_name,
                    "pymonik.attempt": env.attempt,
                    "pymonik.subprocess": True,
                },
                kind="server",
            ):
                result: Any = func(*args, **kwargs)
        _write_result(stdout, b"r", cloudpickle.dumps(result))
        return 0
    except BaseException as e:
        tb = traceback.format_exc()
        msg = f"{type(e).__name__}: {e}\n{tb}"
        try:
            _write_result(stdout, b"e", msg.encode("utf-8", errors="replace"))
        except Exception:
            sys.stderr.write(msg)
        return 1


if __name__ == "__main__":
    sys.exit(main())
