"""Minimal smoke test: submit a single task and print its result.

Easiest:

    export AKCONFIG=/path/to/generated/armonik-cli.yaml
    uv run python examples/hello.py

Or explicit:

    uv run python examples/hello.py --endpoint <host:port> --partition pymonik
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
def double(x: int) -> int:
    return x * 2


@task
def sum_all(xs: list[int]) -> int:
    return sum(xs)

def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=None, help="overrides AKCONFIG if given")
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()
    t0 = time.monotonic()
    with PymonikClient(endpoint=args.endpoint) as client:
        with client.session(partition=args.partition) as s:
            seed = add.spawn(2, 3)
            doubled = double.spawn(seed)
            leaves = add.map(range(8), range(1,9))
            total = sum_all.spawn(leaves)
            print("submitted; waiting for results")

            print("double(add(2, 3)) ->", doubled.result())
            print("sum_all ->", total.result())
    print(f"took {time.monotonic() - t0:.1f}s")

if __name__ == "__main__":
    main()
