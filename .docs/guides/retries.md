# Retries

Two flavours, both opt-in:

- **Cluster-side retries** — `@task(retries=N)` alone. ArmoniK retries
  the task up to N times for any failure (infra crash or user-code
  exception). Cheap; nothing on the client wakes up between attempts.
- **Client-side retries** — `@task(retries=N, retry_on=(...))`. The
  SDK observes the failure type, sleeps a backoff, and re-spawns. The
  cluster's `max_retries` is held at 2 (still covers infra crashes);
  the application retry loop runs in your process.

## Cluster-side: blanket retry on any failure

```python
@task(retries=3)
def flaky(x: int) -> int:
    ...
```

ArmoniK retries up to 3 times. The task is identified by a fresh
`task_id` per attempt; from the client's perspective, the
`Future.result()` either delivers the eventual success or surfaces
the final failure as `TaskFailed`. (To branch on the final outcome
without a `try/except`, use `fut.outcome()` — it returns an `Outcome`
with `.ok` / `.error` / `.value` and never raises on task failure.)

Use this when you don't care *why* a task failed and a re-attempt is
likely to work — transient network errors, temporary resource
contention, ArmoniK pod restarts.

## Client-side: filterable retries with backoff

```python
from pymonik import task

@task(
    retries=5,
    retry_on=(ConnectionError, TimeoutError),
    retry_backoff="exponential",
)
def call_external_api(url: str) -> str:
    ...
```

The SDK retries when the worker raised `ConnectionError` or
`TimeoutError`, up to 5 times, with exponential backoff between
attempts. Other exceptions surface immediately as `TaskFailed` — no
retry.

Backoff strategies:

- `"exponential"` (default) — `0.5, 1.0, 2.0, 4.0, ...` capped at 30s.
- `"linear"` — `0.5, 1.0, 1.5, 2.0, ...`.
- `"constant"` — 1 second between every attempt.
- A number — fixed seconds.
- A callable `attempt -> seconds` — total control.

```python
@task(retries=10, retry_on=(MyTransientError,), retry_backoff=lambda a: 2 ** a + 1)
def very_specific(...): ...
```

`attempt` is 0 for the first retry, 1 for the second, etc.

## When to use which

- **Use cluster retries** when retries are infrastructure-driven:
  ArmoniK pod went away, gRPC blip, agent restart. The cluster handles
  it; your client doesn't need to know.
- **Use client retries** when retries are application-driven: a third-
  party API rate-limited you, a database is recovering, a network
  partition is healing. The application knows the right backoff and
  the right exception types.

You can combine: `@task(retries=5, retry_on=(MyError,))` gives you 5
client-side application retries *plus* the cluster's default 2 infra
retries underneath.

## How a retry surfaces to the user

The client-side retry is invisible to your `await fut` /
`.result()`. The same `Future` object is rewired in place — its
`task_id` and `result_id` change between attempts; awaiters keep
waiting. The `attempt` field on the envelope (visible to the worker
via `pymonik.current().attempt`) lets idempotency-aware code see
which try this is.

The PymoniK logger emits one `task retrying` line per attempt with
the delay and old/new task ids — useful when something is
retry-storming.

## Retries in batches

If you `.map(args)` and one of the N tasks fails, only the failing
one is retried. The other futures in the `FutureList` resolve
normally. Each retry is a single-task re-submission, not a re-batch.

## What doesn't retry

- **Submission failures** — if the gRPC call to submit the batch
  itself raises (control-plane down, auth refused), no retry. The
  exception propagates out of `.spawn()` / `.map()` immediately.
- **Cancelled tasks** — `TaskCancelled` is never retried. Cancellation
  is intentional.
- **`PymonikError`s that aren't subclasses of the listed types** —
  `retry_on` filters strictly. Catch what you mean.
