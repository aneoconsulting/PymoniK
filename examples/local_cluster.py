"""LocalCluster — same task code, no ArmoniK cluster, no Docker.

``pymonik.testing.LocalCluster`` is a drop-in for ``PymonikClient`` that
runs tasks in a thread pool. Useful for unit tests, examples in CI, and
fast iteration on @task functions before committing to a deployment.

What works the same way as the real cluster:

- ``@task`` + ``.spawn()`` / ``.map()`` / ``.with_options(...)``.
- Futures as data dependencies.
- ``pymonik.gather`` / ``as_completed`` (sync and async).
- ``pymonik.current()`` inside tasks (logger, task_id, attempt, cancel check).
- Decorator-level retries (``retry_on=`` / ``retry_backoff=``).
- ``future.cancel()`` / ``session.cancel()``.
- Sub-tasking (returning a Future from a @task is awaited and forwarded).
- Blobs and Materialize (in-memory, file written for materialize).

Run:

    uv run python examples/local_cluster.py
"""

from __future__ import annotations

import asyncio
import time

from pymonik import (
    TaskFailed,
    as_completed,
    current,
    gather,
    task,
)
from pymonik.testing import LocalCluster


# ---- ordinary task ----
@task
def add(a: int, b: int) -> int:
    return a + b


# ---- composition: pass futures as args ----
@task
def sum_all(xs: list[int]) -> int:
    return sum(xs)


# ---- worker-context use ----
@task
def slow_square(x: int) -> int:
    ctx = current()
    ctx.log.info("squaring", x=x, attempt=ctx.attempt)
    time.sleep(0.05)
    return x * x


# ---- retries ----
@task(retries=3, retry_on=(TaskFailed,), retry_backoff="constant")
def flaky() -> str:
    # Use `current().attempt` rather than mutable globals: cloudpickle's
    # round-trip resets module-level state on every attempt, and on the
    # real cluster each retry may land on a different worker pod anyway.
    n = current().attempt
    if n < 3:
        raise RuntimeError(f"failure on attempt {n}")
    return f"ok after {n} attempts"


def sync_demo() -> None:
    print("=== sync demo ===")
    with LocalCluster() as client:
        with client.session(partition="local") as s:
            # 1) basic spawn
            assert add.spawn(2, 3).result() == 5

            # 2) composition via futures-as-args; ArmoniK-style data dep
            leaves = add.map(range(8), range(1, 9))
            total = sum_all.spawn(leaves).result()
            assert total == 64, total
            print(f"  sum DAG     -> {total}")

            # 3) gather over a fan-out — gather returns a FutureList, so the
            # sync values door is .results() (same as Task.map).
            squares = gather(*[slow_square.spawn(i) for i in range(6)]).results()
            print(f"  squares     -> {squares}")

            # 4) retries — `current().attempt` reflects the retry attempt
            assert flaky.spawn().result() == "ok after 3 attempts"
            print("  retried     -> 3 attempts (driven by current().attempt)")
    print()


async def async_demo() -> None:
    print("=== async demo ===")
    async with LocalCluster() as client:
        async with client.session_async(partition="local") as s:
            futs = slow_square.map(range(8))
            t0 = time.monotonic()
            async for done in as_completed(futs):
                v = await done
                print(f"  square ready: {v} ({time.monotonic() - t0:.2f}s)")


if __name__ == "__main__":
    sync_demo()
    asyncio.run(async_demo())
