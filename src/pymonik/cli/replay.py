"""Local replay of an ArmoniK task — MVP.

Faithful replay: download the captured envelope and data dependencies,
optionally rebuild the runtime venv, and re-execute the exact cloudpickled
function locally. The point is to attach a debugger or eyeball a failure
without going through the cluster again.

Subcommands:

    pymonik replay run      <task>   subprocess-isolated re-execution (default-faithful)
    pymonik replay inspect  <task>   dump provenance without executing
    pymonik replay shell    <task>   IPython REPL with func/args/kwargs bound
    pymonik replay pdb      <task>   inline run; pdb.post_mortem on failure

``run`` matches what the cluster does for ``isolate=True`` (fresh
subprocess, fresh sys.modules, EnvSpec recreated). ``shell`` and
``pdb`` run *inline* in the current process — that's the only way to
hand the function over to IPython or attach a debugger to the failing
frame. The trade-off is that inline runs share your sys.modules with
whatever the CLI imported; it's a known compromise for the debugging
modes.

Hard MVP limit: ``task.tail`` / ``task.spawn`` inside the body fail
with a clear error — the agent sidecar isn't present in either mode.
"""

from __future__ import annotations

import contextlib
import sys
import traceback
from inspect import getsource
from types import SimpleNamespace
from typing import Any, Iterator

import cloudpickle
import grpc
import msgspec
import rich_click as click
from armonik.client import ArmoniKResults

from pymonik import context as ctx_mod
from pymonik._internal.env_builder import (
    apply_env_overlay,
    ensure_env,
    restore_env_overlay,
    venv_site_packages,
)
from pymonik._internal.info import TaskInfo
from pymonik._internal.refs import resolve_refs
from pymonik._internal.subprocess_dispatch import run_in_subprocess
from pymonik.client import PymonikClient
from pymonik.context import WorkerContext
from pymonik.envelope import TaskEnvelope
from pymonik.errors import TaskFailed


# ---------- shared helpers ----------


class _NoSidecar:
    """Stand-in for ``WorkerSession`` so any ``.spawn`` / ``.tail`` from
    inside a replayed function raises a clear error instead of an
    opaque ``AttributeError``."""

    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(
            f"replay can't run sub-tasking ({name!r}). That code path needs "
            f"an agent sidecar, which isn't present under `pymonik replay`."
        )


def _download(results: ArmoniKResults, *, result_id: str, session_id: str, kind: str) -> bytes:
    try:
        return results.download_result_data(result_id=result_id, session_id=session_id)
    except grpc.RpcError as e:
        code = getattr(e, "code", lambda: None)()
        if code is grpc.StatusCode.NOT_FOUND:
            raise click.ClickException(
                f"{kind} result {result_id!r} not found on the cluster "
                f"(session {session_id!r}). The task metadata is still here "
                f"but the bytes are gone — most likely the session was "
                f"purged, or the cluster has a retention policy that GC'd "
                f"the payload after task completion."
            )
        details = getattr(e, "details", lambda: "")() or ""
        raise click.ClickException(f"gRPC error downloading {kind} ({code}): {details}")


def _fetch_inputs(
    client: PymonikClient, task_id: str
) -> tuple[TaskInfo, TaskEnvelope, bytes, dict[str, bytes]]:
    task = client.tasks.where(id=task_id).first()
    if task is None:
        raise click.ClickException(f"task {task_id!r} not found")
    if not task.payload_id:
        raise click.ClickException(
            f"task {task_id!r} has no payload_id (status={task.status}); "
            f"nothing to replay"
        )

    assert client._channel is not None
    results = ArmoniKResults(client._channel)

    envelope_bytes = _download(
        results, result_id=task.payload_id, session_id=task.session_id, kind="payload"
    )
    envelope = msgspec.msgpack.decode(envelope_bytes, type=TaskEnvelope)

    data_deps: dict[str, bytes] = {}
    for rid in task.data_dependencies:
        data_deps[rid] = _download(
            results, result_id=rid, session_id=task.session_id, kind="data dep"
        )

    return task, envelope, envelope_bytes, data_deps


def _check_python_match(envelope: TaskEnvelope, mode: str) -> None:
    """Refuse on cross-minor replays so we don't SIGSEGV mid-unpickle."""
    local_py = f"{sys.version_info.major}.{sys.version_info.minor}"
    if envelope.python != local_py:
        raise click.ClickException(
            f"python version mismatch: task ran on {envelope.python}, you're "
            f"on {local_py}. Cloudpickle bytecode isn't cross-minor-compatible. "
            f"Retry under the right interpreter, e.g. "
            f"`uv run --python {envelope.python} pymonik replay {mode} ...`."
        )


def _provenance_line(task: TaskInfo, envelope: TaskEnvelope, mode: str) -> str:
    return (
        f"replay({mode})  task={task.id} session={task.session_id} "
        f"func={envelope.func_name} attempt={envelope.attempt}"
    )


def _format_result(result: Any) -> str:
    """Pretty-print MultiResult fields; otherwise just repr()."""
    from pymonik.multiresult import MultiResult, TailPromise

    if isinstance(result, MultiResult):
        lines = []
        for k, v in result.fields.items():
            if isinstance(v, TailPromise):
                lines.append(
                    f"  {k} = <delegated to {v.task.name} — not executed under replay>"
                )
            else:
                lines.append(f"  {k} = {v!r}")
        return "MultiResult(\n" + "\n".join(lines) + "\n)"
    return repr(result)


@contextlib.contextmanager
def _prep_inline(
    task: TaskInfo, envelope: TaskEnvelope, data_deps: dict[str, bytes]
) -> Iterator[tuple[Any, tuple[Any, ...], dict[str, Any], WorkerContext]]:
    """Mirror the worker's inline dispatch (worker.py:325-405) so the
    yielded ``(func, args, kwargs, ctx)`` can be poked at directly by
    shell / pdb / etc.

    Handles env_spec splice + env overlay, ref resolution, WorkerContext
    binding, and the ``_NoSidecar`` guard against sub-tasking.
    """
    spliced_path: str | None = None
    prior_env: dict[str, str | None] | None = None
    if envelope.env_spec is not None:
        if envelope.env_spec.deps:
            click.echo(f"        building/finding env deps={list(envelope.env_spec.deps)}")
            venv_dir = ensure_env(envelope.env_spec)
            site = str(venv_site_packages(venv_dir))
            if site not in sys.path:
                sys.path.insert(0, site)
                spliced_path = site
        if envelope.env_spec.env:
            prior_env = apply_env_overlay(envelope.env_spec.env)

    try:
        func = cloudpickle.loads(envelope.function_pickle)
        args_raw, kwargs_raw = cloudpickle.loads(envelope.args_pickle)
    except Exception as e:
        raise click.ClickException(f"failed to unpickle function/args: {e!r}")
    args = tuple(resolve_refs(a, data_deps) for a in args_raw)
    kwargs = {k: resolve_refs(v, data_deps) for k, v in kwargs_raw.items()}

    th_stub = SimpleNamespace(task_id=task.id, session_id=task.session_id)
    worker_ctx = WorkerContext(
        th_stub,  # type: ignore[arg-type]
        attempt=envelope.attempt,
        grpc_context=None,
    )
    ctx_token = ctx_mod._set(worker_ctx)
    from pymonik.task import _current_session as _cs

    sess_token = _cs.set(_NoSidecar())  # type: ignore[arg-type]

    try:
        yield func, args, kwargs, worker_ctx
    finally:
        _cs.reset(sess_token)
        ctx_mod._reset(ctx_token)
        if prior_env is not None:
            restore_env_overlay(prior_env)
        if spliced_path is not None:
            try:
                sys.path.remove(spliced_path)
            except ValueError:
                pass


# ---------- click wiring ----------


@click.group("replay")
def replay() -> None:
    """Local replay of an ArmoniK task — faithful re-execution.

    See ``--help`` on each subcommand for what it does. The headline
    distinction: ``run`` is subprocess-isolated, ``shell`` / ``pdb``
    run inline so a debugger can attach.
    """


def _endpoint_opt(f):
    return click.option(
        "--endpoint",
        default=None,
        help="Cluster endpoint (overrides AKCONFIG).",
    )(f)


# ---------- run (subprocess) ----------


@replay.command("run")
@click.argument("task_id")
@_endpoint_opt
def replay_run(task_id: str, endpoint: str | None) -> None:
    """Re-run TASK_ID in a subprocess with its captured inputs.

    Uses the same dispatcher as the cluster's ``isolate=True`` path
    (``subprocess_dispatch.run_in_subprocess``). If the task ran with
    ``env_spec.deps``, the venv is built/reused locally at
    ``~/.cache/pymonik/envs/<env_id>/`` and the child boots from that
    venv's python. Otherwise the child is ``sys.executable``.
    """
    with PymonikClient(endpoint=endpoint) as client:
        task, envelope, envelope_bytes, data_deps = _fetch_inputs(client, task_id)
        _check_python_match(envelope, mode="run")

        click.echo(_provenance_line(task, envelope, "run"))
        if envelope.env_spec is not None and envelope.env_spec.deps:
            click.echo(f"        env deps={list(envelope.env_spec.deps)}")
        if data_deps:
            total = sum(len(v) for v in data_deps.values())
            click.echo(f"        data_deps={len(data_deps)} ({total} bytes)")

        try:
            result_pickle = run_in_subprocess(
                env_spec=envelope.env_spec,
                envelope_bytes=envelope_bytes,
                data_deps=data_deps,
            )
        except TaskFailed as e:
            click.echo("\nreplay raised in subprocess:\n", err=True)
            click.echo(str(e), err=True)
            sys.exit(1)
        except BaseException:
            click.echo("\nreplay infra error:", err=True)
            traceback.print_exc()
            sys.exit(1)

        try:
            result = cloudpickle.loads(result_pickle)
        except Exception as e:
            raise click.ClickException(
                f"subprocess returned {len(result_pickle)} bytes but "
                f"cloudpickle.loads failed: {e!r}"
            )

        click.echo(f"\nresult: {_format_result(result)}")


# ---------- inspect (no execution) ----------


def _trunc(s: str, limit: int = 200) -> str:
    return s if len(s) <= limit else s[:limit] + "…"


@replay.command("inspect")
@click.argument("task_id")
@_endpoint_opt
def replay_inspect(task_id: str, endpoint: str | None) -> None:
    """Dump TASK_ID provenance without executing it.

    Prints task identity, EnvSpec, data-dep sizes, and (when the local
    interpreter matches the task's python) the function source and arg
    repr. Useful for "what was this task" before deciding to debug.
    """
    with PymonikClient(endpoint=endpoint) as client:
        task, envelope, _, data_deps = _fetch_inputs(client, task_id)

        click.echo(f"task        {task.id}")
        click.echo(f"session     {task.session_id}")
        click.echo(f"status      {task.status}")
        if task.partition_id:
            click.echo(f"partition   {task.partition_id}")
        if task.ended_at:
            click.echo(f"ended_at    {task.ended_at}")
        if task.error:
            click.echo(f"error       {_trunc(task.error)}")

        click.echo(f"\nfunction    {envelope.func_name}")
        click.echo(f"attempt     {envelope.attempt}")
        click.echo(f"python      {envelope.python}")
        if envelope.multi_fields:
            click.echo(f"multi_out   {list(envelope.multi_fields)}")
        if envelope.env_spec is not None:
            if envelope.env_spec.deps:
                click.echo(f"deps        {list(envelope.env_spec.deps)}")
            if envelope.env_spec.env:
                click.echo(f"env_vars    {dict(envelope.env_spec.env)}")
            click.echo(f"isolate     {envelope.env_spec.isolate}")

        if envelope.otel_context:
            traceparent = dict(envelope.otel_context).get("traceparent", "")
            if traceparent:
                click.echo(f"trace       {traceparent}")

        total = sum(len(v) for v in data_deps.values())
        click.echo(f"\ndata_deps   {len(data_deps)} ({total} bytes)")
        for rid, b in list(data_deps.items())[:5]:
            click.echo(f"  {rid}  {len(b)} bytes")
        if len(data_deps) > 5:
            click.echo(f"  ... and {len(data_deps) - 5} more")

        local_py = f"{sys.version_info.major}.{sys.version_info.minor}"
        if envelope.python != local_py:
            click.echo(
                f"\n(skipping function/args decode — task python={envelope.python} "
                f"vs local={local_py}; would SIGSEGV)"
            )
            return

        try:
            func = cloudpickle.loads(envelope.function_pickle)
            args_raw, kwargs_raw = cloudpickle.loads(envelope.args_pickle)
        except Exception as e:
            click.echo(f"\n(failed to decode function/args: {e!r})")
            return

        try:
            src = getsource(func)
            click.echo("\nfunction source:")
            click.echo(src)
        except (OSError, TypeError):
            # cloudpickle of a lambda / generated code / function from a
            # module the local source tree doesn't have. Not an error.
            pass

        click.echo("\nargs:")
        for i, a in enumerate(args_raw):
            click.echo(f"  args[{i}]    {type(a).__name__} = {_trunc(repr(a))}")
        for k, v in kwargs_raw.items():
            click.echo(f"  {k!s:8s}  {type(v).__name__} = {_trunc(repr(v))}")


# ---------- shell (inline IPython) ----------


@replay.command("shell")
@click.argument("task_id")
@_endpoint_opt
def replay_shell(task_id: str, endpoint: str | None) -> None:
    """Drop into IPython with func/args/kwargs/ctx bound.

    Runs *inline* (not subprocess) — that's the only way to hand the
    function over to a REPL. Type ``func(*args, **kwargs)`` to execute,
    mutate args between calls, etc.
    """
    try:
        import IPython  # type: ignore[import-not-found]
    except ImportError:
        raise click.ClickException(
            "shell mode needs ipython. Install with `uv pip install ipython` "
            "(or add it as a dev dep)."
        )

    with PymonikClient(endpoint=endpoint) as client:
        task, envelope, _, data_deps = _fetch_inputs(client, task_id)
        _check_python_match(envelope, mode="shell")
        click.echo(_provenance_line(task, envelope, "shell"))

        with _prep_inline(task, envelope, data_deps) as (func, args, kwargs, ctx):
            banner = (
                f"\npymonik replay shell — bound names:\n"
                f"  func    {envelope.func_name}\n"
                f"  args    {len(args)} positional ({[type(a).__name__ for a in args]})\n"
                f"  kwargs  {list(kwargs.keys())}\n"
                f"  ctx     WorkerContext (pymonik.current() returns this)\n"
                f"\nrun: result = func(*args, **kwargs)\n"
            )
            IPython.embed(  # type: ignore[attr-defined]
                banner1=banner,
                user_ns={
                    "func": func,
                    "args": args,
                    "kwargs": kwargs,
                    "ctx": ctx,
                },
            )


# ---------- pdb (inline post-mortem) ----------


@replay.command("pdb")
@click.argument("task_id")
@_endpoint_opt
def replay_pdb(task_id: str, endpoint: str | None) -> None:
    """Re-run TASK_ID inline; drop into pdb.post_mortem on failure.

    Runs *inline* (not subprocess) so pdb can attach to the failing
    frame. On success, prints the result and exits cleanly.
    """
    with PymonikClient(endpoint=endpoint) as client:
        task, envelope, _, data_deps = _fetch_inputs(client, task_id)
        _check_python_match(envelope, mode="pdb")
        click.echo(_provenance_line(task, envelope, "pdb"))

        with _prep_inline(task, envelope, data_deps) as (func, args, kwargs, _ctx):
            try:
                result = func(*args, **kwargs)
            except BaseException:
                click.echo("\nreplay raised:", err=True)
                traceback.print_exc()
                import pdb

                pdb.post_mortem()
                sys.exit(1)

        click.echo(f"\nresult: {_format_result(result)}")
