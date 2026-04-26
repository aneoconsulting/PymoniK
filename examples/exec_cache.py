"""Local execution cache.

Two-knob opt-in:

1. ``PymonikClient(cache=True)`` (or a ``Path``) enables the cache
   *infrastructure* — without this the cache directory is never touched.
2. ``@task(cache=True)`` declares one specific task pure-and-cacheable.

When both are set, ``.spawn()`` / ``.map()`` consult the on-disk cache
*before* submitting. A hit returns a Future that's already resolved
with the cached value — zero RPCs. A miss submits as normal and the
result is written back when it lands.

Caching skips automatically when:

- An arg is a ``Future`` (upstream value not yet known).
- A leaf isn't picklable.

Run twice. First run hits the cluster (or LocalCluster); second run
shows hits and finishes in milliseconds.

    uv run python examples/exec_cache.py
    uv run python examples/exec_cache.py     # second run = hits
    uv run pymonik cache stats               # peek inside
    uv run pymonik cache clear --yes         # wipe between experiments

This example uses LocalCluster so it works without a deployed cluster.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from pymonik import current, task
import pymonik
from pymonik.testing import LocalCluster


@task(cache=True)
def expensive_pure(n: int) -> int:
    """Pretend-expensive computation; deterministic so the cache is valid."""
    current().log.info("running expensive_pure", n=n)
    time.sleep(0.3)  # simulate real work
    return sum(i * i for i in range(n))


@task   # NOT cached — no @task(cache=True)
def cheap_uncached(n: int) -> int:
    return n * 2


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cache-dir",
        default=str(Path.home() / ".cache" / "pymonik-example"),
        help="Cache root for this demo (default keeps it out of the global cache).",
    )
    args = ap.parse_args()

    print(f"using cache at {args.cache_dir}")

    with LocalCluster(cache=args.cache_dir) as client:
        with client.session() as s:
            inputs = [10_000, 20_000, 30_000, 40_000]

            t0 = time.monotonic()
            futures = expensive_pure.map(inputs)
            results = [f.result() for f in futures]
            print(f"  expensive map        -> {results}  ({time.monotonic() - t0:.2f}s)")

            # Same inputs again — should be all hits.
            t0 = time.monotonic()
            again = [expensive_pure.spawn(n).result() for n in inputs]
            print(f"  same inputs again    -> {again}  ({time.monotonic() - t0:.2f}s)")
            assert again == results, "cache returned different value!"

            # New input → miss for that one only.
            t0 = time.monotonic()
            mixed = [expensive_pure.spawn(n).result() for n in [10_000, 50_000, 30_000]]
            print(f"  one new + two cached -> {mixed}  ({time.monotonic() - t0:.2f}s)")

            # Uncached task: never goes to cache regardless of how many times.
            t0 = time.monotonic()
            r = cheap_uncached.spawn(7).result()
            print(f"  cheap_uncached(7)    -> {r}  ({time.monotonic() - t0:.2f}s; not cached)")

            # ``.with_options(cache=False)`` can opt a single call out even
            # when the @task decorator says cache=True.
            t0 = time.monotonic()
            r = expensive_pure.with_options(cache=False).spawn(10_000).result()
            print(f"  cache=False override -> {r}  ({time.monotonic() - t0:.2f}s; bypassed cache)")


if __name__ == "__main__":
    main()
