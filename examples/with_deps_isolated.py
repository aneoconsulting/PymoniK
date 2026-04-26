"""``isolate=True`` — opt into per-task subprocess isolation.

Default deps mode is **in-process splice**: the worker adds the venv's
site-packages to ``sys.path`` and calls the function inline. ~1 ms per
task once warm, but module-level state (and the *single* installed
version of every package) is shared across every task running on that
worker pod.

If you need stronger isolation — concurrent sessions on the same pod
with conflicting deps, or tasks that mutate global module state — pass
``isolate=True`` to spawn a fresh Python interpreter per task. The
trade-off is wall-clock: numpy alone adds ~400-500 ms to every task
(Python startup + numpy import). Heavier stacks (torch, scipy) hurt
more.

Run:

    uv run python examples/with_deps_isolated.py
"""

from __future__ import annotations

import argparse
import time

import numpy as np

import pymonik
from pymonik import PymonikClient, task


@task
def quick_numpy(n: int) -> int:
    return int(np.arange(n).sum())


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--partition", default="pymonik")
    args = ap.parse_args()

    with PymonikClient(endpoint=args.endpoint) as client:
        with client.session(
            partition=args.partition,
            deps=["numpy"],
            isolate=True,
        ) as s:
            futures = [quick_numpy.spawn(i * 1000) for i in range(1, 6)]
            for fut in futures:
                t = time.monotonic()
                v = fut.result(timeout=300)
                print(f"quick_numpy -> {v}  ({time.monotonic() - t:.2f}s)")


if __name__ == "__main__":
    main()
