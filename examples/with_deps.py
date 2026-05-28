"""Runtime pip deps via ``client.session(deps=[...])``.

The worker image only has pymonik; numpy and polars get installed into
a per-deps venv on first use, and reused across every task in this
session (and across sessions/clients that pick the same deps list).

Imports are at module level. cloudpickle ships the function with a
by-name reference to the imported module — on the worker, the
``deps`` venv (spliced into ``sys.path``) makes that import resolve.
The client side needs the dep too: it has to import the module to
build the function in the first place. (To submit deps your client
doesn't have, drop the import inside the task body.)

Run:

    export AKCONFIG=/path/to/generated/armonik-cli.yaml
    uv run python examples/with_deps.py
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import polars as pl

import pymonik
from pymonik import PymonikClient, task


@task
def numpy_stats(n: int) -> dict[str, float]:
    rng = np.random.default_rng(seed=n)
    arr = rng.standard_normal(size=10_000)
    return {
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


@task
def polars_demo() -> str:
    df = pl.DataFrame({"x": [1, 2, 3, 4, 5], "y": [10, 20, 30, 40, 50]})
    return f"polars {pl.__version__}: rows={df.height}, sum_y={df['y'].sum()}"


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    with PymonikClient(endpoint=args.endpoint) as client:
        with client.session(
            partition=args.partition,
            deps=["numpy", "polars"],
        ) as s:
            print("first task pays the install (~tens of seconds)")
            t0 = time.monotonic()
            stats = numpy_stats.spawn(42).result(timeout=600)
            print(f"  numpy_stats(seed=42) -> {stats}")
            print(f"  elapsed: {time.monotonic() - t0:.1f}s")

            print("subsequent tasks: in-process, ~ms per call")
            t1 = time.monotonic()
            polars_msg = polars_demo.spawn().result(timeout=120)
            print(f"  polars_demo() -> {polars_msg}")
            print(f"  elapsed: {time.monotonic() - t1:.3f}s")


if __name__ == "__main__":
    main()
