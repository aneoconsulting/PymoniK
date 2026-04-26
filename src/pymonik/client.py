"""PymonikClient — the connection handle.

Two front doors, one channel:

- **Sync**: ``with PymonikClient() as c:`` — spins up a ``BlockingPortal``
  in ``__enter__`` so sync sessions can run async lifecycle hooks on an
  asyncio loop hosted on a background thread. Drops the portal in
  ``__exit__``.
- **Async**: ``async with PymonikClient() as c:`` — just opens the
  channel; async sessions run on the caller's loop.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import anyio.from_thread
import grpc
from pymonik._internal._logging import get_logger
import yaml

from pymonik._internal import _otel
from pymonik._internal.channel import Credentials, open_channel
from pymonik._internal.exec_cache import ExecCache, default_cache_dir
from pymonik._internal.query import (
    PartitionQuery,
    ResultQuery,
    SessionQuery,
    TaskQuery,
    _make_context,
)
from pymonik.options import EMPTY, TaskOpts
from pymonik.session import Session

log = get_logger(__name__)


class PymonikClient:
    """A connection to an ArmoniK cluster.

    Use as a sync context manager:

        with PymonikClient(endpoint="localhost:5001") as client:
            with client.session(partition="pymonik") as s:
                ...

    For mTLS:

        from pymonik._internal.channel import Credentials

        creds = Credentials(ca="ca.pem", cert="me.pem", key="me.key")
        with PymonikClient(endpoint="grpcs://cluster:5001", credentials=creds) as client:
            ...
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        credentials: Optional[Credentials] = None,
        akconfig: Optional[str | os.PathLike[str]] = None,
        events: bool = True,
        polling_interval: float = 0.5,
        polling_chunk: int = 500,
        spill_threshold: int = 256 * 1024,
        cache: bool | str | os.PathLike[str] | None = None,
        otel: bool | None = None,
        otel_service_name: str = "pymonik",
    ) -> None:
        """Open a client.

        Three ways to configure the endpoint, in order of precedence:

        1. Pass ``endpoint=`` (and optionally ``credentials=``) explicitly.
        2. Pass ``akconfig=/path/to/armonik-cli.yaml`` to load endpoint + CA
           (and optional client cert/key) from a YAML config.
        3. Set the ``AKCONFIG`` env var; the client picks it up automatically.

        Matches the ArmoniK CLI's ``AKCONFIG`` convention so "install, export
        AKCONFIG, go" works out of the box.

        ``events=True`` (default) resolves futures via the ``Events.GetEvents``
        server-stream — latency from result-ready to future-resolved is a few
        ms. Set ``events=False`` to fall back to the polling loop (one
        ``list_results`` RPC every ``polling_interval`` seconds, batched into
        chunks of ``polling_chunk`` ids per RPC). Polling is handy if the
        events stream misbehaves; events are the right default otherwise.

        ``cache`` enables the on-disk execution cache (see
        ``pymonik._internal.exec_cache``). ``None``/``False`` disables it
        entirely. ``True`` enables it under the default location
        (``~/.cache/pymonik``). A path enables it under that directory.
        Per-task opt-in still required: only ``@task(cache=True)`` tasks
        actually consult the cache. Without the per-task flag, the
        infrastructure is wired but unused.

        ``otel`` controls OpenTelemetry tracing. ``None``
        (default) auto-enables when standard OTel env vars are present
        (``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_TRACES_EXPORTER``),
        otherwise stays off. ``True`` enables unconditionally; ``False``
        forces off. Requires ``pip install pymonik[otel]``.
        """
        if endpoint is None:
            cfg_path = akconfig or os.getenv("AKCONFIG")
            if cfg_path is None:
                raise ValueError(
                    "no endpoint given and no AKCONFIG set. "
                    "Either pass endpoint=... or export AKCONFIG=/path/to/armonik-cli.yaml."
                )
            loaded = _load_akconfig(Path(cfg_path))
            endpoint = loaded["endpoint"]
            if credentials is None and loaded.get("certificate_authority"):
                credentials = Credentials(
                    ca=loaded.get("certificate_authority"),
                    cert=loaded.get("client_certificate"),
                    key=loaded.get("client_key"),
                )

        self.endpoint = endpoint
        self.credentials = credentials
        self._events = events
        self._polling_interval = polling_interval
        self._polling_chunk = polling_chunk
        self._spill_threshold = spill_threshold
        self._otel_enabled = _otel.setup(force=otel, service_name=otel_service_name)
        if self._otel_enabled:
            log.info("otel tracing enabled", service=otel_service_name)
        self._cache: ExecCache | None
        if cache is None or cache is False:
            self._cache = None
        elif cache is True:
            self._cache = ExecCache(default_cache_dir())
        else:
            self._cache = ExecCache(Path(cache))
        if self._cache is not None:
            log.info("exec cache enabled", root=str(self._cache.root))
        self._channel: grpc.Channel | None = None
        self._portal: anyio.from_thread.BlockingPortal | None = None
        self._portal_cm: Any = None

    # ---- sync lifecycle ----

    def __enter__(self) -> "PymonikClient":
        self._channel = open_channel(self.endpoint, self.credentials)
        # A background asyncio loop lives here for the duration of the client,
        # so sync Sessions can drive async completion-loop tasks via the portal.
        self._portal_cm = anyio.from_thread.start_blocking_portal(backend="asyncio")
        self._portal = self._portal_cm.__enter__()
        log.info(
            "client connected",
            endpoint=self.endpoint,
            tls=bool(self.credentials),
            mode="sync",
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._portal_cm is not None:
            try:
                self._portal_cm.__exit__(exc_type, exc, tb)
            finally:
                self._portal_cm = None
                self._portal = None
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def session(
        self,
        *,
        partition: str | list[str] | tuple[str, ...],
        default_options: TaskOpts | None = None,
        deps: list[str] | tuple[str, ...] | None = None,
        isolate: bool | None = None,
        index_url: str | None = None,
        env: dict[str, str] | None = None,
        attach_to: str | None = None,
    ) -> Session:
        """Open a session bound to one or more partitions (sync).

        Usage::

            with client.session(partition="pymonik") as s: ...
            with client.session(partition=["cpu", "gpu"]) as s:
                heavy.with_options(partition="gpu").spawn(...)

        ``partition`` accepts a string (single partition; what most users
        want) or a list/tuple of partition ids the session is allowed to
        route into. The first element is the default for tasks that don't
        explicitly select a partition; ``@task(partition="gpu")`` /
        ``.with_options(partition="gpu")`` route to any of the others.
        Selecting a partition not in the session's set raises at submit time.

        ``default_options`` sets session-wide task defaults (retries, timeout,
        priority, partition override). Merge order: session default ← @task(...)
        ← .with_options(...).

        ``deps`` / ``isolate`` / ``index_url`` are sugar for ``default_options``
        — they declare a runtime venv that the worker builds on demand.
        ``env`` adds environment variables to the per-task environment along
        with ``deps`` (different env values produce a distinct ``env_id``
        and a distinct on-disk venv).

        ``attach_to`` attaches to a pre-existing session id instead of
        creating a new one. Tasks submitted in this block land on the
        existing session; the events stream picks up completions for
        any future this client owns. Exiting the ``with`` block does
        **not** ``close_session()`` — the session belongs to whoever
        created it. Useful for picking up where another process left
        off, or for sharing a session across multiple driver scripts.
        ``partition`` is still required (it backstops per-task
        validation client-side); it should match what the original
        ``create_session`` declared. ``default_options`` is informational
        only when attached — the cluster-side defaults were fixed at
        create time.
        """
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
        return Session(
            self,
            partition=partition,
            default_options=merged,
            use_events=self._events,
            polling_interval=self._polling_interval,
            polling_chunk=self._polling_chunk,
            spill_threshold=self._spill_threshold,
            cache=self._cache,
            attach_to=attach_to,
        )

    # ---- async lifecycle ----

    async def __aenter__(self) -> "PymonikClient":
        self._channel = open_channel(self.endpoint, self.credentials)
        log.info(
            "client connected",
            endpoint=self.endpoint,
            tls=bool(self.credentials),
            mode="async",
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    # ---- introspection ----

    def _qctx(self):
        if self._channel is None:
            raise RuntimeError("client is not connected — open it first")
        return _make_context(self._channel)

    @property
    def tasks(self) -> TaskQuery:
        """All tasks visible to this client. Cluster-wide."""
        return TaskQuery(self._qctx())

    @property
    def sessions(self) -> SessionQuery:
        """All sessions visible to this client."""
        return SessionQuery(self._qctx())

    @property
    def results(self) -> ResultQuery:
        """All results across the cluster.

        Mutation verbs (``delete()`` / ``download()`` / ``download_to()``)
        require a session-scoped query; call from
        ``session.results`` instead.
        """
        return ResultQuery(self._qctx())

    @property
    def partitions(self) -> PartitionQuery:
        """All partitions on the cluster. Read-only."""
        return PartitionQuery(self._qctx())

    @asynccontextmanager
    async def session_async(
        self,
        *,
        partition: str | list[str] | tuple[str, ...],
        default_options: TaskOpts | None = None,
        deps: list[str] | tuple[str, ...] | None = None,
        isolate: bool | None = None,
        index_url: str | None = None,
        env: dict[str, str] | None = None,
        attach_to: str | None = None,
    ):
        """Open a session bound to one or more partitions (async).

        Mirrors :meth:`session`. See its docstring for ``partition`` /
        ``deps`` / ``env`` / ``attach_to`` semantics.
        """
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
        sess = Session(
            self,
            partition=partition,
            default_options=merged,
            use_events=self._events,
            polling_interval=self._polling_interval,
            polling_chunk=self._polling_chunk,
            spill_threshold=self._spill_threshold,
            cache=self._cache,
            attach_to=attach_to,
        )
        async with sess:
            yield sess


def _load_akconfig(path: Path) -> dict[str, str]:
    """Parse the ArmoniK CLI's YAML config."""
    with path.open("r") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    out = {str(k): str(v) for k, v in raw.items() if v is not None}
    if "endpoint" not in out:
        raise ValueError(f"{path}: missing 'endpoint' key")
    return out
