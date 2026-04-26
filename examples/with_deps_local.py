"""Runtime deps under ``LocalCluster`` — same code path as the cluster.

The local backend exercises the same ``ensure_env`` + dispatcher that
runs on real workers, so this is also a self-contained integration
test for the Path A pipeline. Useful for debugging install failures
(PyPI lookups, conflicts) without round-tripping a real cluster.

``isolate=False`` (the default) is shown first: install once, then
every subsequent task is essentially free. The ``isolate=True``
section shows the cost of the subprocess path for comparison.

Run:

    uv run python examples/with_deps_local.py
"""

from __future__ import annotations

import time

import numpy as np

import pymonik
from pymonik import task
from pymonik.testing import LocalCluster


@task
def numpy_sum(n: int) -> int:
    return int(np.arange(n).sum())


@task(deps=["numpy"])
def numpy_sum_per_task(n: int) -> int:
    """Same payload, but deps come from the @task decorator rather than
    the session — useful for "this one task needs numpy, the rest
    of the session doesn't".
    """
    return int(np.arange(n).sum())


def main() -> None:
    pymonik.enable_logging()

    print("default isolate=False (in-process splice)")
    with LocalCluster() as client:
        with client.session(deps=["numpy"]) as s:
            t0 = time.monotonic()
            v = numpy_sum.spawn(1_000).result(timeout=600)
            print(
                f"  numpy_sum(1000) -> {v}  ({time.monotonic() - t0:.1f}s, first call pays install)"
            )
            t1 = time.monotonic()
            v = numpy_sum.spawn(10_000).result(timeout=60)
            print(
                f"  numpy_sum(10000) -> {v}  ({time.monotonic() - t1:.4f}s, in-process — ~free)"
            )

    print("\nopt-in isolate=True (subprocess per task)")
    with LocalCluster() as client:
        with client.session(deps=["numpy"], isolate=True) as s:
            t0 = time.monotonic()
            v = numpy_sum.spawn(1_000).result(timeout=600)
            print(f"  numpy_sum(1000) -> {v}  ({time.monotonic() - t0:.2f}s, subprocess startup + numpy import)")
            t1 = time.monotonic()
            v = numpy_sum.spawn(10_000).result(timeout=60)
            print(f"  numpy_sum(10000) -> {v}  ({time.monotonic() - t1:.2f}s, subprocess startup + numpy import)")

    print("\nper-task deps via @task(deps=[...])")
    with LocalCluster() as client:
        with client.session() as s:
            v = numpy_sum_per_task.spawn(500).result(timeout=600)
            print(f"  numpy_sum_per_task(500) -> {v}")


if __name__ == "__main__":
    main()
