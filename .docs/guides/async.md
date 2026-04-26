# Async

PymoniK exposes the same surface twice: a sync API for scripts and
notebooks, an async API for asyncio / trio applications. The async
version is built on `anyio`, so the same code runs under either
backend.

## Sync vs async, side by side

```python
# Sync
with PymonikClient() as client:
    with client.session(partition="pymonik") as s:
        result = add.spawn(2, 3).result(timeout=30)

# Async
async with PymonikClient() as client:
    async with client.session_async(partition="pymonik") as s:
        result = await add.spawn(2, 3)
```

`Future` works in either context: call `result()` to block, `await
fut` to suspend.

## When to use the async API

- Your application is already async (FastAPI, an asyncio service, a
  trio application).
- You need to interleave PymoniK submissions with other I/O without
  blocking.
- You want structured concurrency primitives (`anyio.create_task_group`)
  to bound parallelism, propagate cancellation, and fan in errors as
  `ExceptionGroup`s.

If your code is plain sync (a notebook, a CLI, a batch job), the sync
API is simpler and gives you the same performance.

## Awaiting many futures

Plain `await` on a `FutureList` is one common pattern:

```python
async with client.session_async(partition="pymonik") as s:
    futures = add.map(range(32), range(1, 33))
    results = await futures.results_async(timeout=60)
```

Or stream completions as they arrive (order: ready first):

```python
from pymonik import as_completed

async with client.session_async(partition="pymonik") as s:
    futures = work.map(many_args)
    async for done in as_completed(futures):
        value = await done
        # process as it lands
```

Or collect with structured fan-in errors:

```python
from pymonik import gather, TaskFailed

async with client.session_async(partition="pymonik") as s:
    futures = work.map(many_args)
    try:
        results = await gather(futures)
    except* TaskFailed as eg:
        for failed in eg.exceptions:
            log.warning("retry candidate", task=failed.task_id)
```

The `try/except*` syntax (PEP 654) lets you catch one exception type
out of an `ExceptionGroup` while letting others propagate — the right
shape for "tell me about all the failures, then re-raise the rest."

## Submission off the event loop

`.spawn()` and `.map()` stay sync from async code — they're a few
gRPC calls, returning a `Future` is fast. If your event loop is
sensitive to even small blocks, use `.spawn_async()` / `.map_async()`
which offload the submission RPCs to a worker thread:

```python
async with client.session_async(partition="pymonik") as s:
    fut = await heavy_work.spawn_async(big_arg)
    result = await fut
```

For typical workloads the difference is invisible — submission
latency is dominated by network round-trip, not local CPU.

## Both backends: asyncio and trio

`async with PymonikClient()` works on either:

```python
# asyncio
import asyncio
asyncio.run(my_pipeline())

# trio
import trio
trio.run(my_pipeline)
```

Internally PymoniK's completion machinery currently uses an asyncio
loop (the trio backend bridges through `anyio`'s blocking portal).
The user-facing primitives are anyio.Event and anyio.create_task_group,
so user code reads the same on both.

## Cancellation

A cancel scope around a `await fut` propagates to the cluster:

```python
import anyio

async with anyio.create_task_group() as tg:
    fut = work.spawn(...)
    with anyio.move_on_after(30):           # 30s deadline
        result = await fut
    if not fut.done:
        # The cancel scope exited; PymoniK has already issued
        # CancelTasks on the cluster. The future is resolved with
        # TaskCancelled.
        ...
```

(Today the
client-side cancel works; the cluster-side teardown is best-effort.)

## When you're sync but the rest of your app is async

If you're embedding PymoniK in a long-running asyncio service:

```python
# Use the async API directly on the service's loop:
async def my_handler():
    async with PymonikClient() as client:
        async with client.session_async(...) as s:
            return await work.spawn(...)
```

Don't open `with PymonikClient()` (sync) on a thread inside an
asyncio service — that spins up a second asyncio loop in a portal
thread. It works, it's just slower than using the async API
directly.

## Mixing async and sync across processes

The wire format is the same. A sync client can submit work whose
results an async client awaits in another process — they share a
session id and a result id is the only handle you need.
