"""Pipeline example: futures as arguments, no client-side blocking.

Demonstrates that ``sum_all.spawn(add.spawn(...), add.spawn(...))`` builds
an ArmoniK DAG via data_dependencies — the sum task runs only after its
inputs complete, and the client blocks only on the terminal .result().

    uv run python examples/pipeline.py --partition pymonikv1
"""

from __future__ import annotations

import argparse
import time

from pymonik import PymonikClient, task
import pymonik


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def sum_all(xs: list[int]) -> int:
    return sum(xs)


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    ap.add_argument("--n", type=int, default=32, help="leaf additions")
    args = ap.parse_args()

    t0 = time.monotonic()
    with PymonikClient() as client:
        with client.session(partition=args.partition) as s:
            # Leaves: N parallel adds.
            leaves = add.map(range(args.n), range(1, args.n + 1))

            # Fan-in: sum_all depends on every leaf. Submitted immediately;
            # runs only after all leaves complete. Client never touches the
            # intermediate values.
            total = sum_all.spawn(leaves)
            print(f"submitted DAG: {args.n} leaves + 1 reducer")
            print("total ->", total.result(timeout=300))
    print(f"took {time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()
