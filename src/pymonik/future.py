"""Future[T] — a handle to a task's not-yet-arrived result.

Composes with other tasks *without* blocking the client: pass a Future as
an argument to another ``.spawn()`` and the new task runs with a
``data_dependencies`` edge in ArmoniK. ``.result()`` / ``await`` are only
needed on terminal results.

Resolution bridge: the completion loop (events stream or polling) runs in
a thread and calls :meth:`_resolve_ok` / :meth:`_resolve_error`. Those set
a ``threading.Event`` (for sync ``.result()``) and, if any awaiter has
registered an ``asyncio.Event`` on this future, wake it via
``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import cloudpickle

from pymonik._internal import _otel
from pymonik.errors import PymonikError, TaskFailed, TaskTimeout


_WORKER_STUB_BLOCK_MSG = (
    "cannot .result() / await a Future from inside a @task — ArmoniK tasks "
    "are ephemeral and must not block on other tasks. Pass the Future to "
    "another .spawn() (creates a data_dependencies edge so ArmoniK runs the "
    "next task once this one completes), or use task.tail(args) to delegate "
    "your output to a sub-task."
)

if TYPE_CHECKING:
    from pymonik.session import Session

T = TypeVar("T")


class Future(Generic[T]):
    """A handle to the (eventual) result of a spawned task.

    Two wait primitives cooperate:

    - ``_done`` (threading.Event): sync ``.result()`` waits on this.
    - ``_aio_done`` (asyncio.Event, lazy): async ``await`` waits on this.

    The completion thread sets both. Clients don't see the split.
    """

    __slots__ = (
        "_session",
        "_task_id",
        "_result_id",
        "_done",
        "_aio_done",
        "_aio_loop",
        "_outcome",
        "_error",
        # True when the future was created inside a worker via WorkerSession.
        # Such futures have no poller, so .result() / await would hang. We
        # raise a typed error instead — ArmoniK tasks are ephemeral and
        # blocking inside one is illegal.
        "_is_worker_stub",
        # Client-side retry policy. None when retries aren't configured.
        # Tuple shape: (task, args, kwargs, max_retries, on_types, backoff_fn)
        # — see Session._submit_many. Read by _resolve_error to decide
        # whether to suppress the error and trigger a re-submit.
        "_retry_state",
        "_retry_attempt",
        # Set when this future was spawned with caching enabled. The
        # session's resolver writes the cloudpickled result to the
        # ExecCache under this key on success.
        "_cache_key",
    )

    def __init__(self, session: "Session", task_id: str, result_id: str) -> None:
        self._session = session
        self._task_id = task_id
        self._result_id = result_id
        self._done = threading.Event()
        # Built lazily on first await so we don't need an event loop just to
        # construct the Future. Stored here so threaded resolvers can notify.
        self._aio_done: asyncio.Event | None = None
        self._aio_loop: asyncio.AbstractEventLoop | None = None
        self._outcome: Any = None
        self._error: PymonikError | None = None
        self._is_worker_stub: bool = False
        self._retry_state: Any = None
        self._retry_attempt: int = 0
        self._cache_key: str | None = None

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def result_id(self) -> str:
        return self._result_id

    @property
    def done(self) -> bool:
        return self._done.is_set()

    # ---- internal: resolved by the session completion loop (thread) ----
    def _resolve_ok(self, raw_bytes: bytes) -> None:
        if self._done.is_set():
            return
        try:
            self._outcome = cloudpickle.loads(raw_bytes)
        except Exception as e:
            self._error = TaskFailed(self._task_id, f"could not unpickle result: {e!r}")
        self._done.set()
        self._wake_async()

    def _resolve_error(self, err: PymonikError) -> None:
        if self._done.is_set():
            return
        # Retry path: if a matching policy is configured and budget remains,
        # suppress this error, trigger re-submission, and leave _done unset.
        rs = self._retry_state
        if rs is not None:
            _task, _args, _kwargs, max_retries, on_types, _backoff = rs
            if isinstance(err, on_types) and self._retry_attempt < max_retries:
                self._retry_attempt += 1
                self._session._schedule_retry(self, attempt=self._retry_attempt)
                return
        self._error = err
        self._done.set()
        self._wake_async()

    def _wake_async(self) -> None:
        """Called from the completion thread; wake any async awaiter."""
        if self._aio_done is None or self._aio_loop is None:
            return
        try:
            # call_soon_threadsafe is the one asyncio primitive that is safe
            # to call from another thread — it schedules .set() on the loop.
            self._aio_loop.call_soon_threadsafe(self._aio_done.set)
        except RuntimeError:
            # Loop has closed before resolution landed; sync waiters unaffected.
            pass

    # ---- public sync wait ----
    def result(self, timeout: float | None = None) -> T:
        if self._is_worker_stub:
            raise PymonikError(_WORKER_STUB_BLOCK_MSG)
        with _otel.start_span(
            "pymonik.future.wait",
            attrs={"pymonik.task_id": self._task_id, "pymonik.mode": "sync"},
            kind="client",
        ):
            got = self._done.wait(timeout=timeout)
            if not got:
                raise TaskTimeout(self._task_id)
            if self._error is not None:
                raise self._error
            return self._outcome  # type: ignore[no-any-return]

    # ---- public sync wait (no value, no error raise) ----
    def wait(self, timeout: float | None = None) -> "Future[T]":
        """Block until this future is resolved (success or failure). Return self.

        Unlike :meth:`result`, this does not raise on task failure /
        cancellation and does not deliver the value. Use it when you
        want to synchronise without surfacing the value or its errors —
        either because you don't need the value yet, or because you
        plan to inspect ``.done`` and the future's state explicitly.

        Chains naturally with :meth:`result` for a two-step access:

            value = fut.wait(timeout=60).result()

        Raises :class:`pymonik.TaskTimeout` if the timeout elapses
        without resolution.
        """
        if self._is_worker_stub:
            raise PymonikError(_WORKER_STUB_BLOCK_MSG)
        got = self._done.wait(timeout=timeout)
        if not got:
            raise TaskTimeout(self._task_id)
        return self

    # ---- public async wait ----
    async def _await(self, timeout: float | None = None) -> T:
        if self._is_worker_stub:
            raise PymonikError(_WORKER_STUB_BLOCK_MSG)
        # Lazy construction — only pay the cost when someone actually awaits.
        if self._aio_done is None:
            self._aio_loop = asyncio.get_running_loop()
            self._aio_done = asyncio.Event()
            # If the result landed before we got here, make sure the event
            # is already set so the upcoming wait() returns immediately.
            if self._done.is_set():
                self._aio_done.set()

        try:
            if timeout is None:
                await self._aio_done.wait()
            else:
                await asyncio.wait_for(self._aio_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TaskTimeout(self._task_id) from None

        if self._error is not None:
            raise self._error
        return self._outcome  # type: ignore[no-any-return]

    async def wait_async(self, timeout: float | None = None) -> "Future[T]":
        """Async sibling of :meth:`wait`. Block until resolved; return self.

        Does not surface the value or raise on task failure — same
        rationale as :meth:`wait`.
        """
        if self._is_worker_stub:
            raise PymonikError(_WORKER_STUB_BLOCK_MSG)
        if self._aio_done is None:
            self._aio_loop = asyncio.get_running_loop()
            self._aio_done = asyncio.Event()
            if self._done.is_set():
                self._aio_done.set()
        try:
            if timeout is None:
                await self._aio_done.wait()
            else:
                await asyncio.wait_for(self._aio_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TaskTimeout(self._task_id) from None
        return self

    # ---- internal construction (cache hit) ----
    @classmethod
    def _new_cached(cls, session: Any, cached_bytes: bytes) -> "Future[Any]":
        """Build a Future that's already resolved with ``cached_bytes``.

        The caller (Session / LocalSession) uses this when the local
        execution cache hits — no submission, no RPC, the user awaits a
        future that immediately yields the cloudpickled value.

        Bypassing ``__init__`` keeps the slot-init tax low and lets us
        commit to a state that's already done.
        """
        fut: Future[Any] = cls.__new__(cls)
        fut._session = session
        fut._task_id = "cached"
        fut._result_id = "cached"
        fut._done = threading.Event()
        fut._aio_done = None
        fut._aio_loop = None
        fut._outcome = None
        fut._error = None
        fut._is_worker_stub = False
        fut._retry_state = None
        fut._retry_attempt = 0
        fut._cache_key = None
        fut._resolve_ok(cached_bytes)
        return fut

    # ---- internal construction (worker-stub) ----
    @classmethod
    def _new_worker_stub(
        cls,
        session: Any,
        task_id: str,
        result_id: str,
    ) -> "Future[Any]":
        """Build a Future for a task spawned *inside* a worker.

        No poller exists worker-side, so these futures cannot be awaited.
        They're only useful as arguments to further ``.spawn()`` calls
        (building data_dependencies edges).
        """
        fut: Future[Any] = cls.__new__(cls)
        fut._session = session
        fut._task_id = task_id
        fut._result_id = result_id
        fut._done = threading.Event()
        fut._aio_done = None
        fut._aio_loop = None
        fut._outcome = None
        fut._error = None
        fut._is_worker_stub = True
        fut._retry_state = None
        fut._retry_attempt = 0
        fut._cache_key = None
        return fut

    # ---- cancellation (client side) ----
    def cancel(self) -> None:
        """Ask ArmoniK to cancel the task this future points to.

        Fires CancelTasks on the cluster and resolves this future locally
        with ``TaskCancelled``. The task may run a bit longer on the worker
        before it observes the cancel; the result is discarded either way.
        """
        if self._is_worker_stub:
            raise PymonikError(_WORKER_STUB_BLOCK_MSG)
        if self._done.is_set():
            return
        # Session owns the gRPC plumbing.
        self._session._cancel_future(self)

    def __await__(self):
        return self._await().__await__()

    def __repr__(self) -> str:
        state = "done" if self._done.is_set() else "pending"
        return f"<Future task_id={self._task_id!r} {state}>"

    def _repr_html_(self) -> str:
        from pymonik._internal.notebook import future_html

        return future_html(self)

    def _ipython_display_(self) -> None:
        from pymonik._internal.notebook import display_live, future_html

        # Worker-stub futures have no poller, so live updates would spin
        # forever — fall back to a static paint.
        if self._is_worker_stub:
            try:
                from IPython.display import HTML, display  # type: ignore
            except Exception:
                print(repr(self))
                return
            display(HTML(future_html(self)))
            return
        display_live(self, future_html, futures=[self])


class FutureList(Generic[T]):
    """A batch of futures from ``Task.map`` / ``starmap``."""

    __slots__ = ("_futures",)

    def __init__(self, futures: list[Future[T]]) -> None:
        self._futures = futures

    def __iter__(self):
        return iter(self._futures)

    def __getitem__(self, idx):
        return self._futures[idx]

    def __len__(self) -> int:
        return len(self._futures)

    def results(self, timeout: float | None = None) -> list[T]:
        """Sync: block until every future resolves; return them in submission order."""
        return [f.result(timeout=timeout) for f in self._futures]

    async def results_async(self, timeout: float | None = None) -> list[T]:
        """Async: await every future concurrently; return them in submission order."""
        return await asyncio.gather(*(f._await(timeout) for f in self._futures))

    def wait(self, timeout: float | None = None) -> "FutureList[T]":
        """Block until every future is resolved; return self.

        Doesn't surface values or raise on task failures — see
        :meth:`Future.wait`. Apply the timeout per future, same as
        :meth:`results`.
        """
        for f in self._futures:
            f.wait(timeout=timeout)
        return self

    async def wait_async(self, timeout: float | None = None) -> "FutureList[T]":
        """Async sibling of :meth:`wait`."""
        await asyncio.gather(*(f.wait_async(timeout) for f in self._futures))
        return self

    def __repr__(self) -> str:
        done = sum(1 for f in self._futures if f.done)
        return f"<FutureList {done}/{len(self._futures)} done>"

    def _repr_html_(self) -> str:
        from pymonik._internal.notebook import future_list_html

        return future_list_html(self)

    def _ipython_display_(self) -> None:
        from pymonik._internal.notebook import display_live, future_list_html

        if any(f._is_worker_stub for f in self._futures):
            try:
                from IPython.display import HTML, display  # type: ignore
            except Exception:
                print(repr(self))
                return
            display(HTML(future_list_html(self)))
            return
        display_live(self, future_list_html, futures=list(self._futures))


class MultiResultView:
    """The resolved view of a ``MultiResult``. Supports both attribute
    and dict-style access on the named fields::

        out = split.spawn(7)
        view = out.result()
        view.double           # 14
        view["double"]        # 14
        dict(view)            # {"double": 14, "triple": 21}
        view == {"double": 14, "triple": 21}    # True

    Iteration yields field names (matching ``dict``'s default).
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name: str) -> Any:
        # Reach through to _data; raise AttributeError on miss so
        # introspection (hasattr, etc.) behaves correctly.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(
                f"{name!r} is not a field of this MultiResult "
                f"(available: {list(object.__getattribute__(self, '_data'))})"
            )

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MultiResultView):
            return self._data == other._data
        if isinstance(other, dict):
            return self._data == other
        return NotImplemented

    def __repr__(self) -> str:
        parts = ", ".join(f"{k}={v!r}" for k, v in self._data.items())
        return f"MultiResultView({parts})"


class MultiResultHandle:
    """Handle returned by ``.spawn()`` for a multi-output ``@task``.

    Field access (``handle.field_name``) returns a :class:`Future` for
    that one ArmoniK output. Awaiting / ``.result()`` on the handle as
    a whole blocks on every field and returns a :class:`MultiResultView`.

    See :class:`pymonik.MultiResult`.
    """

    __slots__ = ("_session", "_task_id", "_field_to_future")

    def __init__(
        self,
        session: Any,
        task_id: str,
        field_to_future: dict[str, Future[Any]],
    ) -> None:
        self._session = session
        self._task_id = task_id
        self._field_to_future = field_to_future

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self._field_to_future.keys())

    def __getattr__(self, name: str) -> Future[Any]:
        # ``__slots__``-bound names are handled by normal attribute access;
        # this hook only fires for non-slot attribute reads.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(self, "_field_to_future")[name]
        except KeyError:
            fields = ", ".join(object.__getattribute__(self, "_field_to_future"))
            raise AttributeError(
                f"{name!r} is not a field of this MultiResultHandle "
                f"(available: {fields})"
            )

    def __getitem__(self, name: str) -> Future[Any]:
        return self._field_to_future[name]

    def __iter__(self):
        return iter(self._field_to_future.values())

    def result(self, timeout: float | None = None) -> MultiResultView:
        """Block until every field resolves; return a :class:`MultiResultView`.

        The view supports attribute access (``view.double``) and
        dict-style access (``view["double"]``); compares equal to a
        plain ``dict`` of the same values.
        """
        return MultiResultView(
            {f: fut.result(timeout=timeout) for f, fut in self._field_to_future.items()}
        )

    def wait(self, timeout: float | None = None) -> "MultiResultHandle":
        """Block until every field is resolved; return self.

        Doesn't surface values or raise on task failures — see
        :meth:`Future.wait`.
        """
        for fut in self._field_to_future.values():
            fut.wait(timeout=timeout)
        return self

    async def _await(self, timeout: float | None = None) -> MultiResultView:
        results = await asyncio.gather(
            *(fut._await(timeout) for fut in self._field_to_future.values())
        )
        return MultiResultView(dict(zip(self._field_to_future.keys(), results)))

    async def wait_async(self, timeout: float | None = None) -> "MultiResultHandle":
        """Async sibling of :meth:`wait`."""
        await asyncio.gather(
            *(fut.wait_async(timeout) for fut in self._field_to_future.values())
        )
        return self

    def __await__(self):
        return self._await().__await__()

    def cancel(self) -> None:
        """Cancel the task that produces all of this handle's outputs.

        ArmoniK's CancelTasks operates per-task; cancelling one field
        wouldn't make sense (one task writes all fields). All field
        Futures resolve to ``TaskCancelled``.
        """
        # All field futures share the same task_id, so we only need one
        # cancel_tasks RPC. Issue it via the first non-done future, then
        # resolve the rest locally without re-issuing.
        from pymonik.errors import TaskCancelled

        any_fut = next(iter(self._field_to_future.values()))
        if not any_fut.done:
            any_fut.cancel()  # one CancelTasks RPC + local resolution
        for fut in self._field_to_future.values():
            if not fut.done:
                fut._resolve_error(TaskCancelled(fut.task_id))

    @property
    def done(self) -> bool:
        return all(fut.done for fut in self._field_to_future.values())

    def __repr__(self) -> str:
        done = sum(1 for f in self._field_to_future.values() if f.done)
        n = len(self._field_to_future)
        return (
            f"<MultiResultHandle task_id={self._task_id!r} "
            f"{done}/{n} fields done: {list(self._field_to_future)}>"
        )
