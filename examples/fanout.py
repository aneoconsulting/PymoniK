"""Fan-out example: submit N tasks in parallel, collect results.

Demonstrates that the session's background poller can resolve many
futures at once.

    uv run python examples/fanout.py --endpoint localhost:5001 --partition pymonik --n 20
"""

from __future__ import annotations

import argparse
import random
import time

from pymonik import PymonikClient, task
import pymonik


@task
def estimate_pi_partial(num_samples: int) -> tuple[int, int]:
    hits = 0
    for _ in range(num_samples):
        x, y = random.random(), random.random()
        if x * x + y * y <= 1.0:
            hits += 1
    return hits, num_samples


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="localhost:5001")
    ap.add_argument("--partition", default="pymonik")
    ap.add_argument("--n", type=int, default=20, help="number of worker tasks")
    ap.add_argument("--samples", type=int, default=200_000, help="samples per task")
    args = ap.parse_args()

    t0 = time.monotonic()
    with PymonikClient(endpoint=args.endpoint) as client:
        with client.session(partition=args.partition) as s:
            futures = [estimate_pi_partial.spawn(args.samples) for _ in range(args.n)]
            print(f"submitted {args.n} tasks; waiting")
            total_hits = 0
            total_samples = 0
            for f in futures:
                hits, samples = f.result(timeout=300)
                total_hits += hits
                total_samples += samples
            pi = 4 * total_hits / total_samples
    elapsed = time.monotonic() - t0
    print(f"pi ≈ {pi:.6f}  ({total_samples} samples, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
