"""Parent-side dispatcher for the per-deps subprocess path.

When the envelope carries ``env_spec.deps`` and ``env_spec.isolate=True``
(default), the worker hands the task off to a child Python interpreter
booted from the env's venv. This module owns:

- starting the child via :mod:`subprocess` with ``PYTHONPATH`` rigged so
  the child can ``import pymonik`` without the venv needing pymonik
  itself installed (we use the worker's pymonik, the user's deps);
- writing the framed envelope + data_deps to its stdin (see
  :mod:`pymonik._internal.task_runner` for the protocol);
- reading the framed result back from stdout;
- timing out / killing the child if the worker is cancelled;
- surfacing stderr on failure so users see install / runtime tracebacks.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Mapping

from pymonik._internal._logging import get_logger
from pymonik._internal.env_builder import _venv_python, ensure_env, venv_site_packages
from pymonik.envelope import EnvSpec
from pymonik.errors import PymonikError, TaskFailed

log = get_logger(__name__)


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def _frame_input(envelope_bytes: bytes, data_deps: Mapping[str, bytes]) -> bytes:
    parts: list[bytes] = [_u32(len(envelope_bytes)), envelope_bytes, _u32(len(data_deps))]
    for k, v in data_deps.items():
        kb = k.encode("utf-8")
        parts.append(_u32(len(kb)))
        parts.append(kb)
        parts.append(_u32(len(v)))
        parts.append(v)
    return b"".join(parts)


def _read_result(stream) -> tuple[bytes, bytes]:
    tag = stream.read(1)
    if not tag:
        raise PymonikError("subprocess produced no result on stdout")
    length_bytes = stream.read(4)
    if len(length_bytes) != 4:
        raise PymonikError("subprocess truncated result frame (length)")
    (length,) = struct.unpack(">I", length_bytes)
    payload = b""
    remaining = length
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise PymonikError(
                f"subprocess truncated result frame (payload, "
                f"got {length - remaining} of {length})"
            )
        payload += chunk
        remaining -= len(chunk)
    return tag, payload


def _parent_pythonpath() -> str:
    """``PYTHONPATH`` for the child so it can ``import pymonik``.

    The venv only contains the user's deps. ``pymonik``, ``cloudpickle``
    and ``msgspec`` live in the worker process's site-packages; we
    forward those so the runner module can run without us installing
    pymonik into every venv.
    """
    paths = [p for p in sys.path if p and not p.endswith("site-packages/pymonik")]
    # Drop the cwd entry — child shouldn't pick up the parent's working dir.
    paths = [p for p in paths if p not in (".", "")]
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        return os.pathsep.join([existing] + paths)
    return os.pathsep.join(paths)


def run_in_subprocess(
    *,
    env_spec: EnvSpec | None,
    envelope_bytes: bytes,
    data_deps: Mapping[str, bytes],
    timeout_s: float | None = None,
    task_id: str = "",
    session_id: str = "",
) -> bytes:
    """Build (or reuse) the venv, dispatch the task, return cloudpickled result.

    When ``env_spec`` is ``None`` (or has no deps), skip the venv build
    and fork ``sys.executable`` directly — the child still runs the
    same task_runner pipeline, just against the parent's interpreter
    rather than a deps-isolated venv. Used by ``pymonik replay`` for
    tasks that ran on the worker's base interpreter. (Eventually this can also let us easily change
    Python versions on remote env.. good side-effect?)

    Raises :class:`TaskFailed` with the child's traceback on user-code
    failure; raises :class:`PymonikError` on infra failure (env build,
    subprocess crash, framing mismatch).
    """
    if env_spec is not None and env_spec.deps:
        venv_dir = ensure_env(env_spec)
        py = _venv_python(venv_dir)
        if not py.exists():
            raise PymonikError(f"venv python missing after build: {py}")
    else:
        py = Path(sys.executable)

    env = os.environ.copy()
    env["PYTHONPATH"] = _parent_pythonpath()
    # Don't inherit a __PYVENV_LAUNCHER__ that would point to the worker's
    # interpreter — the child must use its venv python.
    env.pop("__PYVENV_LAUNCHER__", None)
    # Suppress user-site so the child stays isolated to the venv.
    env["PYTHONNOUSERSITE"] = "1"
    # Apply EnvSpec.env on top — user vars win.
    if env_spec is not None:
        for k, v in env_spec.env:
            env[k] = v
    # Task identity for the child's worker context (pymonik.current() /
    # injected ctx: Ctx). Set last so the framework's ids always win.
    # The child can't observe cancellation or reach the agent sidecar, so
    # its context's cancel/sidecar surface is inert — see task_runner.
    if task_id:
        env["PYMONIK_TASK_ID"] = task_id
    if session_id:
        env["PYMONIK_SESSION_ID"] = session_id

    proc = subprocess.Popen(
        [str(py), "-m", "pymonik._internal.task_runner"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=os.getcwd(),
    )

    framed = _frame_input(envelope_bytes, data_deps)

    # Capture stderr in a background thread so a chatty child can't deadlock
    # us by filling the pipe.
    stderr_chunks: list[bytes] = []
    assert proc.stderr is not None
    stderr_pipe = proc.stderr

    def _drain_stderr():
        for chunk in iter(lambda: stderr_pipe.read(65536), b""):
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(framed)
            proc.stdin.close()
        except BrokenPipeError as e:
            # Child died before consuming input. Wait for stderr and report.
            proc.wait(timeout=5)
            stderr_thread.join(timeout=2)
            raise PymonikError(
                f"subprocess died before reading input: "
                f"{b''.join(stderr_chunks).decode(errors='replace')}"
            ) from e

        try:
            tag, payload = _read_result(proc.stdout)
        except Exception:
            proc.wait(timeout=5)
            stderr_thread.join(timeout=2)
            stderr_text = b"".join(stderr_chunks).decode(errors="replace")
            raise PymonikError(
                f"subprocess produced no usable result (rc={proc.returncode}); "
                f"stderr:\n{stderr_text}"
            )

        rc = proc.wait(timeout=timeout_s)
        stderr_thread.join(timeout=2)

        if tag == b"e":
            raise TaskFailed(
                "subprocess", payload.decode("utf-8", errors="replace")
            )
        if tag != b"r":
            raise PymonikError(f"subprocess sent unknown tag: {tag!r}")
        if rc != 0:
            log.warning("subprocess returned non-zero", rc=rc)
        return payload
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


def run_in_process_with_splice(
    *,
    env_spec: EnvSpec,
    runner,
):
    """``isolate=False`` escape hatch: build the env, splice site-packages, run.

    Imports of names already loaded into ``sys.modules`` win; the splice
    only helps for new imports. Concurrent sessions on the same pod with
    conflicting deps will see the first-imported version of a package.
    """
    venv_dir = ensure_env(env_spec)
    site = venv_site_packages(venv_dir)
    site_str = str(site)
    inserted = False
    if site_str not in sys.path:
        sys.path.insert(0, site_str)
        inserted = True
    try:
        return runner()
    finally:
        if inserted:
            try:
                sys.path.remove(site_str)
            except ValueError:
                pass
