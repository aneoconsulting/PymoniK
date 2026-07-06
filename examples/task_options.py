"""Per-task and per-call option overrides.

Demonstrates decorator options, ``.with_options(...)``, and the
session-default → @task → .with_options precedence.

In a multi-partition deployment you'd use ``.with_options(partition=...)``
to route a task to a specific partition. The quick-deploy here only has
``pymonikv1``, so the partition switch is shown in code but would
otherwise target e.g. ``pymonik_gpu``.

    uv run python examples/task_options.py --partition pymonikv1
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from pymonik import PymonikClient, TaskOpts, current, task
import pymonik


# Decorator-level options. Merged with session default; overridable per-call.
@task(retries=3, timeout=timedelta(seconds=30), priority=5)
def heavy_compute(n: int) -> int:
    ctx = current()
    ctx.log.info("heavy_compute", attempt=ctx.attempt, n=n)
    total = 0
    for i in range(n):
        total += i * i
    return total


@task
def echo(value: str) -> str:
    return f"echo: {value}"


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    # Session default: applies to every task unless overridden.
    session_default = TaskOpts(priority=1, timeout=120)

    with PymonikClient() as client:
        with client.session(partition=args.partition, default_options=session_default) as s:
            # Uses @task(retries=3, timeout=30s, priority=5) merged over session default.
            f1 = heavy_compute.spawn(10_000)
            print("heavy_compute ->", f1.result(timeout=60))

            # .with_options overrides per-call: a shorter timeout just for this spawn.
            f2 = heavy_compute.with_options(timeout=5, priority=10).spawn(1_000)
            print("heavy_compute (per-call override) ->", f2.result(timeout=60))

            # .with_options returns a new Task; the original is untouched.
            assert heavy_compute.opts.priority == 5
            f3 = echo.spawn("plain decorator, no options")
            print("echo ->", f3.result(timeout=60))


if __name__ == "__main__":
    main()
