"""Async entry points — ``async with PymonikClient()`` and ``await future``.

Mirrors ``examples/hello.py`` but runs on the user's asyncio loop.
Submission stays sync (spawn returns a Future), waiting is asynchronous.

    uv run python examples/async_hello.py --partition <pymonik-partition>
"""

from __future__ import annotations

import argparse
import asyncio
import time

from pymonik import PymonikClient, task
import pymonik


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def double(x: int) -> int:
    return x * 2


@task
def sum_all(xs: list[int]) -> int:
    return sum(xs)


async def main(partition: str) -> None:
    pymonik.enable_logging()
    t0 = time.monotonic()
    async with PymonikClient() as client:
        async with client.session_async(partition=partition) as s:
            # Composition: spawn is sync, await is async. Submission returns
            # immediately; ArmoniK holds `doubled` and `total` in PENDING
            # via data_dependencies until their inputs complete.
            seed = add.spawn(2, 3)
            doubled = double.spawn(seed)          # depends on seed
            leaves = add.map(range(8), range(1, 9))
            total = sum_all.spawn(leaves)         # depends on all leaves

            # Await two separate DAG terminals concurrently via asyncio.gather.
            a, b = await asyncio.gather(doubled, total)
            print(f"doubled(2+3) = {a}")
            print(f"sum(1,3,5,...,15) = {b}")

    print(f"took {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()
    asyncio.run(main(args.partition))
