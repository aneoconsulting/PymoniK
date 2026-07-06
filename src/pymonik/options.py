"""Task-level options with sane merge semantics.

User-facing fields use Pythonic names (``partition``, ``retries``, ``timeout``,
``priority``) and translate to ``armonik.common.TaskOptions`` at submission
time. Merge order at the call site is:

    session default  ←  @task(...)  ←  .with_options(...)

``None`` means "inherit"; a concrete value means "override".

Retry semantics
---------------
``retries=N`` alone → ArmoniK ``max_retries=N`` (cluster-side, blanket
retries for infra failures and user-code errors alike) — what most
users want.

When ``retry_on=(SomeError, ...)`` is also set, ``retries`` becomes the
*client-side* retry budget — the SDK observes the failure type, optionally
sleeps a backoff, and re-spawns. Cluster ``max_retries`` is held at the
default 2 in that case (still covers infra crashes), and `retries` no
longer leaks into the per-task `TaskOptions` sent to ArmoniK.

This split lets users have either "blind retry on the cluster" (cheap,
no filtering) or "selective retry on the client" (filterable, with
backoff) without two competing knobs.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import timedelta
from typing import Callable, Optional, Tuple, Type, Union

from armonik.common import TaskOptions

_TimeoutLike = Union[timedelta, float, int]
BackoffSpec = Union[str, float, int, Callable[[int], float], None]


def _as_timedelta(value: Optional[_TimeoutLike]) -> Optional[timedelta]:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    return timedelta(seconds=float(value))


def _exponential(attempt: int) -> float:
    # 0.5, 1.0, 2.0, 4.0, ...; capped at 30 s to avoid runaway delays.
    return min(30.0, 0.5 * (2**attempt))


def _linear(attempt: int) -> float:
    return 0.5 * (attempt + 1)


def _constant(_attempt: int) -> float:
    return 1.0


def resolve_backoff(spec: BackoffSpec) -> Callable[[int], float]:
    """Turn a user-provided backoff spec into a callable ``attempt -> seconds``.

    ``attempt`` is 0 for the *first* retry, 1 for the second, etc.
    """
    if spec is None or spec == "exponential":
        return _exponential
    if callable(spec):
        return spec
    if isinstance(spec, (int, float)):
        seconds = float(spec)
        return lambda _attempt: seconds
    if isinstance(spec, str):
        if spec == "linear":
            return _linear
        if spec == "constant":
            return _constant
    raise ValueError(
        f"unknown backoff spec {spec!r}; expected 'exponential' / 'linear' / "
        f"'constant', a number of seconds, or a callable(attempt) -> seconds"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskOpts:
    """Python-ergonomic view of ``armonik.common.TaskOptions`` plus client-side
    retry knobs.

    All fields are optional. ``merge(self, other)`` returns a new TaskOpts
    where ``other``'s non-None fields override ``self``'s.
    """

    partition: Optional[str] = None
    retries: Optional[int] = None
    timeout: Optional[_TimeoutLike] = None
    priority: Optional[int] = None
    # Client-side retry: tuple of exception types that should be retried.
    # When set, ``retries`` becomes the client retry budget (default 3 if
    # not specified) and cluster-side max_retries is pinned to 2.
    retry_on: Optional[Tuple[Type[BaseException], ...]] = None
    # Backoff strategy between client-side retries. See ``resolve_backoff``.
    retry_backoff: BackoffSpec = None
    # Local execution cache opt-in. None inherits (effectively off — the
    # client-level cache is opt-in *infrastructure*; per-task ``cache=True``
    # opts that task in). True = cache this task; False = don't (overrides
    # any ambient default).
    cache: Optional[bool] = None
    # Cache identity override. When set, the structural cache
    # key uses this string as the function's identity instead of hashing
    # its source — bump it to force a recompute when the function's
    # *behaviour* changed in a way the source hash can't see (e.g. a
    # helper it calls changed). None = derive identity from source.
    cache_version: Optional[str] = None
    # Optional local-value cache. When True, a terminal
    # value materialised via ``.result()`` is also persisted to local
    # disk so a later run returns it with zero cluster contact.
    cache_locally: Optional[bool] = None
    # Runtime pip dependencies. List of PEP-508 specifiers (e.g.
    # ``("numpy>=2", "polars")``). Hashed into an env_id; the worker
    # builds (or reuses) a venv per env_id and runs the task against it.
    # Empty / None = no extra deps (worker uses its own site-packages).
    deps: Optional[Tuple[str, ...]] = None
    # When ``deps`` is non-empty: False (default) splices the venv's
    # site-packages into the worker process via ``sys.path`` — ~1 ms
    # per task once warm, but module state leaks across tasks on the
    # same pod. True spawns a fresh Python per task against the env's
    # venv (full isolation, ~400-500 ms startup with a numpy-class dep).
    isolate: Optional[bool] = None
    # Optional private PyPI-style index for the worker's ``uv pip install``.
    index_url: Optional[str] = None
    # Per-task environment variables applied alongside ``deps``. Merged
    # key-wise. Different env values produce a distinct ``env_id`` so two
    # sessions with the same deps but different env vars do NOT share the
    # on-disk venv (deliberate — env vars often select install behaviour,
    # CUDA build, private index credentials, etc.).
    env: Optional[dict[str, str]] = None
    # Free-form string map handed through to TaskOptions.options; used for
    # things like OTel trace context and application tags. Merged key-wise.
    options: Optional[dict[str, str]] = None

    def merge(self, other: "TaskOpts") -> "TaskOpts":
        patch: dict = {}
        for f in fields(self):
            v = getattr(other, f.name)
            if v is None:
                continue
            if f.name == "options":
                merged = dict(self.options or {})
                merged.update(v)
                patch["options"] = merged
            elif f.name == "env":
                # key-wise merge: per-task env adds to/overrides session env
                merged = dict(self.env or {})
                merged.update(v)
                patch["env"] = merged
            else:
                patch[f.name] = v
        return replace(self, **patch)

    def to_armonik(self, *, default_partition: str) -> TaskOptions:
        """Build an armonik.common.TaskOptions for submission.

        ``default_partition`` backstops ``partition`` — ArmoniK rejects a
        TaskOptions without a partition_id, and a task without an explicit
        partition should run on the session's default.

        When ``retry_on`` is set, ``max_retries`` is fixed at 2 (covers
        cluster infra failures only) — the client owns the application
        retry loop. Otherwise ``retries`` flows straight through.
        """
        if self.retry_on is not None:
            max_retries = 2
        else:
            max_retries = self.retries if self.retries is not None else 2
        return TaskOptions(
            max_duration=_as_timedelta(self.timeout) or timedelta(minutes=10),
            priority=self.priority if self.priority is not None else 1,
            max_retries=max_retries,
            partition_id=self.partition or default_partition,
            options=dict(self.options) if self.options else {},
        )


EMPTY = TaskOpts()
