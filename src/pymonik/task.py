"""@task decorator, Task wrapper, .spawn / .map / .tail / .with_options.

The decorator preserves the wrapped function's signature via ParamSpec so
``add(2, 3)`` (local call) and ``add.spawn(2, 3)`` (remote submission) both
type-check. Options merge left-to-right:

    session default  ←  @task(...)  ←  .with_options(...)

``.spawn()`` returns a ``Future[T]``. ``.map()`` returns a
``FutureList[T]``. ``.tail()`` returns a lazily-submitted
``TailPromise[T]`` for sub-tasking. Passing futures as args is
transparently rewritten into ArmoniK data dependencies (see
``_internal/refs.py``).

Multi-output tasks return a :class:`pymonik.MultiResult`. The decorator
walks the function body's AST to find every ``MultiResult(...)`` literal
and extracts the field set, so the submission pipeline can pre-allocate
the right number of ``expected_output_ids`` per task. Inconsistent
shapes between branches raise at decoration time.
"""

from __future__ import annotations

import contextvars
import functools
from typing import Any, Callable, Generic, Iterable, ParamSpec, TypeVar, overload

import anyio

from pymonik._internal._ast_introspect import extract_multi_fields
from pymonik._internal.protocols import SubmittableSession
from pymonik.errors import NotInSessionError, PymonikError
from pymonik.future import Future, FutureList
from pymonik.multiresult import TailPromise
from pymonik.options import EMPTY, TaskOpts

P = ParamSpec("P")
R = TypeVar("R")


# The "currently open session" — set by Session.__enter__ /
# WorkerSession.__init__ (via worker.py) / LocalSession.__enter__.
# Held here rather than in session.py to avoid an import cycle.
_current_session: contextvars.ContextVar["SubmittableSession | None"] = (
    contextvars.ContextVar("_current_session", default=None)
)


def current_session() -> SubmittableSession:
    s = _current_session.get()
    if s is None:
        raise NotInSessionError(
            "no session open. Wrap your spawn() calls in `with client.session(): ...`."
        )
    return s


class Task(Generic[P, R]):
    """A function wrapped for ArmoniK submission."""

    __slots__ = ("func", "name", "opts", "multi_fields")

    def __init__(
        self,
        func: Callable[P, R],
        *,
        name: str | None = None,
        opts: TaskOpts = EMPTY,
        multi_fields: tuple[str, ...] | None = None,
    ) -> None:
        self.func: Callable[P, R] = func
        self.name = name or getattr(func, "__name__", "<anonymous>")
        self.opts = opts
        # Sorted field names for multi-output tasks. ``None`` for plain
        # single-output tasks. Set by the @task decorator via AST
        # introspection (or via ``@task(outputs=(...))``).
        self.multi_fields = multi_fields

    # Local call — just runs the function.
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self.func(*args, **kwargs)

    def with_options(
        self,
        *,
        partition: str | None = None,
        retries: int | None = None,
        timeout: Any = None,
        priority: int | None = None,
        retry_on: tuple[type[BaseException], ...] | None = None,
        retry_backoff: Any = None,
        cache: bool | None = None,
        deps: list[str] | tuple[str, ...] | None = None,
        isolate: bool | None = None,
        index_url: str | None = None,
        env: dict[str, str] | None = None,
        options: dict[str, str] | None = None,
    ) -> "Task[P, R]":
        """Return a new Task with overridden options. Never mutates self."""
        patch = TaskOpts(
            partition=partition,
            retries=retries,
            timeout=timeout,
            priority=priority,
            retry_on=retry_on,
            retry_backoff=retry_backoff,
            cache=cache,
            deps=tuple(deps) if deps is not None else None,
            isolate=isolate,
            index_url=index_url,
            env=dict(env) if env is not None else None,
            options=options,
        )
        return Task(
            self.func,
            name=self.name,
            opts=self.opts.merge(patch),
            multi_fields=self.multi_fields,
        )

    def spawn(self, *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        """Submit this task for remote execution. Returns a Future.

        For multi-output tasks (those that return :class:`MultiResult`),
        ``.spawn()`` returns a :class:`MultiResultHandle` whose
        ``.field_name`` attributes are individual Futures. Awaiting the
        handle blocks on every field; awaiting one field blocks only on
        that one.

        This is sync. From async code it blocks the event loop briefly
        (~few ms of gRPC) while the submission happens. For tight inner
        loops where that matters, use :meth:`spawn_async`.
        """
        if "_delegate" in kwargs:
            raise PymonikError(
                "_delegate=True is no longer supported. Use task.tail(*args) "
                "for tail-call sub-tasking."
            )
        session = current_session()
        return session._submit_one(self, args, kwargs)

    def tail(self, *args: P.args, **kwargs: P.kwargs) -> "TailPromise[R]":
        """Build a lazily-submitted tail-call promise.

        ``return other.tail(args)`` from a ``@task`` body delegates the
        parent's expected output to ``other``. Inside a ``MultiResult``
        field, ``other.tail(args)`` delegates that one field's output.

        The promise is not submitted until the parent ``@task``'s
        worker dispatcher binds it to an output id. Awaiting a
        ``TailPromise`` directly raises — use :meth:`spawn` if you
        want to submit and await.
        """
        return TailPromise(self, args, kwargs)

    def map(self, *iterables: Iterable[Any]) -> FutureList[R]:
        """Apply this task elementwise across one or more iterables.

        Mirrors Python's built-in :func:`map`:

            square.map([1, 2, 3])           -> square(1), square(2), square(3)
            add.map([1, 3], [2, 4])         -> add(1, 2), add(3, 4)

        The N iterables are zipped (stopping at the shortest) and one
        task is submitted per zipped tuple. Submission is batched into a
        single RPC. See :meth:`starmap` for the
        already-have-tuples-of-args shape, and :meth:`map_async` for
        the offloaded variant.
        """
        if not iterables:
            raise TypeError("Task.map requires at least one iterable")
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
            (args, {}) for args in zip(*iterables)
        ]
        session = current_session()
        return session._submit_many(self, calls)

    def starmap(self, args_iter: Iterable[tuple[Any, ...]]) -> FutureList[R]:
        """Apply this task to each tuple after unpacking it as positional args.

        Mirrors :func:`itertools.starmap`:

            add.starmap([(1, 2), (3, 4)])   -> add(1, 2), add(3, 4)

        Use this when you already have tuples-of-args; use :meth:`map`
        when you have one (or more) parallel iterables.
        """
        session = current_session()
        return session._submit_many(self, list(args_iter))

    async def spawn_async(self, *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        """Async sibling of :meth:`spawn`.

        Offloads the (otherwise blocking) gRPC submission to a worker
        thread via :func:`anyio.to_thread.run_sync` so the calling event
        loop keeps running. ContextVars (the current session, OTel
        context) are propagated automatically by anyio.

        The returned ``Future`` is the same shape as ``spawn()``'s — use
        ``await fut`` to get the value.
        """
        if "_delegate" in kwargs:
            raise PymonikError(
                "_delegate=True is no longer supported. Use task.tail(*args) "
                "for tail-call sub-tasking."
            )
        session = current_session()
        fn = functools.partial(session._submit_one, self, args, kwargs)
        return await anyio.to_thread.run_sync(fn)

    async def map_async(self, *iterables: Iterable[Any]) -> FutureList[R]:
        """Async sibling of :meth:`map`.

        Offloads the batched submission RPC off the event loop. Useful
        when ``map`` is called with many tasks (the per-batch round-trip
        gets bigger with N).
        """
        if not iterables:
            raise TypeError("Task.map_async requires at least one iterable")
        items: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
            (args, {}) for args in zip(*iterables)
        ]
        session = current_session()
        fn = functools.partial(session._submit_many, self, items)
        return await anyio.to_thread.run_sync(fn)

    async def starmap_async(
        self, args_iter: Iterable[tuple[Any, ...]]
    ) -> FutureList[R]:
        """Async sibling of :meth:`starmap`."""
        session = current_session()
        items = list(args_iter)
        fn = functools.partial(session._submit_many, self, items)
        return await anyio.to_thread.run_sync(fn)

    def __repr__(self) -> str:
        return f"<Task {self.name} opts={self.opts!r}>"


# --- decorator ---

@overload
def task(func: Callable[P, R], /) -> Task[P, R]: ...
@overload
def task(
    *,
    partition: str | None = None,
    retries: int | None = None,
    timeout: Any = None,
    priority: int | None = None,
    retry_on: tuple[type[BaseException], ...] | None = None,
    retry_backoff: Any = None,
    cache: bool | None = None,
    deps: list[str] | tuple[str, ...] | None = None,
    isolate: bool | None = None,
    index_url: str | None = None,
    env: dict[str, str] | None = None,
    options: dict[str, str] | None = None,
    outputs: tuple[str, ...] | list[str] | None = None,
) -> Callable[[Callable[P, R]], Task[P, R]]: ...
def task(func: Callable[P, R] | None = None, /, **kwargs: Any) -> Any:
    """Decorate a function for ArmoniK submission.

    Usage:

        @task
        def add(a: int, b: int) -> int:
            return a + b

        @task(partition="gpu", retries=3, timeout=timedelta(minutes=5))
        def render(scene: Scene) -> bytes:
            ...

        @task(retries=5, retry_on=(ConnectionError,), retry_backoff="exponential")
        def flaky_call(): ...

    Plain ``retries=N`` is cluster-side: ArmoniK retries up to N times.
    Adding ``retry_on=(...)`` switches to *client-side* retry — same
    budget, but the SDK observes the failure type, sleeps the configured
    backoff, and re-spawns. Cluster ``max_retries`` then sits at 2 to
    cover infra failures.

    For multi-output tasks (those returning ``MultiResult``), the
    decorator extracts the field set from the function body's AST. If
    your construction is dynamic (a helper function, a comprehension)
    or you'd rather declare it explicitly, pass
    ``outputs=("field_a", "field_b", ...)``.

    Decorator-level options are merged with session defaults and with
    ``.with_options(...)`` overrides at submission time.
    """
    deps = kwargs.pop("deps", None)
    env = kwargs.pop("env", None)
    explicit_outputs = kwargs.pop("outputs", None)
    opts = TaskOpts(
        partition=kwargs.pop("partition", None),
        retries=kwargs.pop("retries", None),
        timeout=kwargs.pop("timeout", None),
        priority=kwargs.pop("priority", None),
        retry_on=kwargs.pop("retry_on", None),
        retry_backoff=kwargs.pop("retry_backoff", None),
        cache=kwargs.pop("cache", None),
        deps=tuple(deps) if deps is not None else None,
        isolate=kwargs.pop("isolate", None),
        index_url=kwargs.pop("index_url", None),
        env=dict(env) if env is not None else None,
        options=kwargs.pop("options", None),
    )
    if kwargs:
        raise TypeError(f"@task got unexpected kwargs: {sorted(kwargs)}")

    def _wrap(f: Callable[P, R]) -> Task[P, R]:
        if explicit_outputs is not None:
            from pymonik.multiresult import MultiResult as _MR

            multi_fields: tuple[str, ...] | None = tuple(sorted(explicit_outputs))
            bad = set(multi_fields) & _MR._RESERVED_FIELD_NAMES
            if bad:
                raise PymonikError(
                    f"@task {f.__name__!r}: outputs={sorted(bad)} collide "
                    f"with MultiResultHandle attributes. Reserved names: "
                    f"{sorted(_MR._RESERVED_FIELD_NAMES)}."
                )
            bad_uscore = {n for n in multi_fields if n.startswith("_")}
            if bad_uscore:
                raise PymonikError(
                    f"@task {f.__name__!r}: outputs={sorted(bad_uscore)} "
                    f"are invalid: underscore-prefixed names are reserved."
                )
        else:
            multi_fields = extract_multi_fields(f)
        if multi_fields is not None and opts.cache is True:
            raise PymonikError(
                f"@task {f.__name__!r}: cache=True is not compatible with "
                f"multi-output tasks. The execution cache stores one bytes "
                f"blob per task; per-field caching for MultiResult isn't "
                f"implemented."
            )
        return Task(f, opts=opts, multi_fields=multi_fields)

    if func is not None:
        return _wrap(func)
    return _wrap
