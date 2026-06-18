# Async

PymoniK exposes the same surface twice. You choose your world once — `with`
for sync, `async with` for async — and from then on the only difference is
the `await` keyword. A `Future` is a single handle with two doors: block it
(`fut.result()`) or await it (`await fut`).

The async API runs on **asyncio** today. (Native trio support is planned but
not yet wired — `await fut` needs a running asyncio loop.)

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

Submission is identical in both worlds — `.spawn()` / `.map()` return a
handle synchronously (submission is a fast gRPC call). Only the *wait*
differs: `.result()` blocks the thread, `await` suspends the coroutine.

## The two things you wait for: value vs outcome

`await fut` (or `fut.result()`) gives you the **value**, raising on failure.
When you'd rather **settle without raising** — branch on success/failure, or
wait without paying to download the result — use the outcome door:

```python
# Sync: outcome() never raises on task failure
oc = add.spawn(2, 3).outcome()
print(oc.value if oc.ok else oc.error)

# Async: try/except around await is the idiomatic "settle one"
try:
    value = await add.spawn(2, 3)
except TaskFailed as e:
    ...
```

An `Outcome` carries `.ok`, `.error`, and a lazily-materialised `.value`
(downloaded only when you actually read it).

## When to use the async API

- Your application is already async (FastAPI, an asyncio service).
- You need to interleave PymoniK submissions with other I/O without blocking.
- You want structured-concurrency timeouts / cancellation around your awaits.

If your code is plain sync (a notebook, a CLI, a batch job), the sync API is
simpler and gives you the same performance.

## Awaiting many futures

`await` a `FutureList` to get every value, in submission order:

```python
async with client.session_async(partition="pymonik") as s:
    futures = add.map(range(32), range(1, 33))
    results = await futures            # list of values
```

Stream completions as they land (ready first):

```python
from pymonik import as_completed

async with client.session_async(partition="pymonik") as s:
    futures = work.map(many_args)
    async for done in as_completed(futures):
        value = await done             # process as it lands
```

`as_completed` is one object that works with both `for` and `async for` —
pick the loop your world speaks.

## Fan-in with `gather`

`gather(...)` flattens any mix of futures and `FutureList`s into a single
`FutureList` — so you wait on it exactly like one from `Task.map`:
`await gather(...)` (async) or `gather(...).results()` (sync) for the values
in order, raising the first failure:

```python
from pymonik import gather

async with client.session_async(partition="pymonik") as s:
    results = await gather(work.spawn(1), work.map([2,3,4,5]))
```

To collect **every** result and failure instead of stopping at the first
error, settle the batch with `.outcomes()` — a list of `Outcome`s, nothing
raised:

```python
with client.session(partition="pymonik") as s:
    for o in work.map(many_args).outcomes():
        if not o.ok:
            log.warning("task failed", error=o.error)
```

`.outcomes()` / `.results()` are the blocking (sync) doors. From async code,
`await gather(...)` gives the values; to settle without raising there, use
`try`/`except` around `await fut` or iterate `as_completed`.

## Timeouts

Sync code passes `timeout=` (it has no structured alternative); async code
uses the loop's native structured timeout, which composes over many awaits:

```python
# Sync
value = fut.result(timeout=30)

# Async
import asyncio
async with asyncio.timeout(30):        # or anyio.fail_after(30)
    value = await fut
```

## Submission off the event loop

`.spawn()` / `.map()` stay sync from async code — they're a few gRPC calls,
and returning a `Future` is fast. If your loop is sensitive to even small
blocks, `.spawn_async()` / `.map_async()` offload the submission RPCs to a
worker thread:

```python
async with client.session_async(partition="pymonik") as s:
    fut = await heavy_work.spawn_async(big_arg)
    result = await fut
```

For typical workloads the difference is invisible, as submission latency is
dominated by the network round-trip, not local CPU.


## When you're sync but the rest of your app is async

If you're embedding PymoniK in a long-running asyncio service, use the async
API directly on the service's loop:

```python
async def my_handler():
    async with PymonikClient() as client:
        async with client.session_async(...) as s:
            return await work.spawn(...)
```

Don't open `with PymonikClient()` (sync) on a thread inside an asyncio
service — that spins up a second asyncio loop in a portal thread. It works,
it's just slower than the async API. (And calling `.result()` from inside a
running loop now raises, to stop you doing it by accident.)

## Mixing async and sync across processes

The wire format is the same. A sync client can submit work whose results an
async client awaits in another process — they share a session id, and a
result id is the only handle you need.
