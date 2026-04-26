"""Async fan-in: ``pymonik.gather`` and ``pymonik.as_completed``.

Same flavour as ``asyncio.gather`` / ``asyncio.as_completed`` but takes
``Future`` / ``FutureList`` directly. Two demos in one script:

1. Submit a fan-out, gather everything in submission order.
2. Submit a fan-out, consume results as-they-complete with the typed
   Future yielded back.

    uv run python examples/gather_async.py --partition pymonikv1
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time

from pymonik import PymonikClient, as_completed, gather, task
import pymonik


@task
def slow_double(x: int) -> int:
    # Random short delay so the fan-out has interesting completion order.
    import time as _t
    _t.sleep(0.1 + 0.6 * random.random())
    return x * 2


async def main(partition: str, n: int) -> None:
    pymonik.enable_logging()
    async with PymonikClient() as client:
        async with client.session_async(partition=partition) as s:
            # ---- gather() ----
            print(f"submitting {n} tasks for gather()")
            t0 = time.monotonic()
            futs = slow_double.map(range(n))
            results = await gather(futs)  # results in submission order
            print(f"  gather -> {results}  ({time.monotonic() - t0:.1f}s)")

            # ---- as_completed() ----
            print(f"submitting {n} more for as_completed()")
            t0 = time.monotonic()
            futs2 = slow_double.map(range(100, 100 + n))
            received: list[int] = []
            async for done in as_completed(futs2):
                value = await done   # the typed Future is yielded back
                received.append(value)
                print(f"  +{value:>3}  (running for {time.monotonic() - t0:.2f}s)")
            print(f"  total {sum(received)}  ({time.monotonic() - t0:.1f}s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(main(args.partition, args.n))
