"""Estimate π via parallel Monte-Carlo sampling.

Map N parallel Monte-Carlo estimates, then reduce via a single task whose
inputs are the fan-out futures. Client blocks only on the terminal result.

    uv run examples/estimate_pi.py --partition <pymonik-partition> --n 32 --samples 200000
"""

from __future__ import annotations

import argparse
import random
import time

from pymonik import PymonikClient, current, task
import pymonik


@task
def estimate_pi_partial(num_samples: int) -> tuple[int, int]:
    # pymonik.current() gives structured-logging + task/session ids on the worker.
    current().log.info("shard start", samples=num_samples)
    hits = 0
    for _ in range(num_samples):
        x, y = random.random(), random.random()
        if x * x + y * y <= 1.0:
            hits += 1
    return hits, num_samples


@task
def reduce_pi(partials: list[tuple[int, int]]) -> float:
    hits = sum(h for h, _ in partials)
    samples = sum(n for _, n in partials)
    return 4 * hits / samples


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    ap.add_argument("--n", type=int, default=32)
    ap.add_argument("--samples", type=int, default=200_000)
    args = ap.parse_args()

    t0 = time.monotonic()
    with PymonikClient() as client, client.session(partition=args.partition) as s:
        shards = estimate_pi_partial.map([args.samples] * args.n)
        pi = reduce_pi.spawn(shards).result(timeout=300)
    print(f"pi ≈ {pi:.6f}  ({args.n * args.samples} samples, {time.monotonic() - t0:.1f}s)")


if __name__ == "__main__":
    main()
