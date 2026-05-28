"""Cancellation — client-initiated, cooperatively honoured on the worker.

Two flows:

1. ``future.cancel()`` — cancels a single task via ArmoniK ``CancelTasks``.
   The future resolves locally with :class:`TaskCancelled` immediately;
   the task may run briefly longer on the worker until it checks in via
   ``pymonik.current().cancel_if_requested()``. 
   # NOTE!!! This behavior should be implemented in the `armonik` python package 

2. ``session.cancel()`` — cancels every in-flight task in the session
   via ``CancelSession``. All pending futures resolve with
   :class:`TaskCancelled`.

Cooperative on the worker side: the ``@task`` body must periodically call
``pymonik.current().cancel_if_requested()``. A task that never checks
runs to ``max_duration`` regardless of cluster state.

    uv run python examples/cancellation.py --partition pymonikv1
"""

from __future__ import annotations

import argparse
import threading
import time

from pymonik import PymonikClient, TaskCancelled, current, task
import pymonik


@task
def slow(steps: int) -> int:
    """Cooperative long task. Checks in every iteration."""
    ctx = current()
    for i in range(steps):
        ctx.cancel_if_requested()  # raises TaskCancelled if so
        if i % 5 == 0:
            ctx.log.info("tick", i=i, steps=steps)
        time.sleep(0.3)
    return steps


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    with PymonikClient() as client:
        with client.session(partition=args.partition) as s:
            # ---- 1. cancel a single future ----
            print("test 1: single future.cancel()")
            fut = slow.spawn(30)  # would take ~9 s

            def cancel_after(sec: float):
                time.sleep(sec)
                print(f"  client: cancelling after {sec}s")
                fut.cancel()

            threading.Thread(target=cancel_after, args=(2.0,), daemon=True).start()
            t0 = time.monotonic()
            try:
                fut.result(timeout=30)
                print("  UNEXPECTED success")
            except TaskCancelled as e:
                print(f"  cancelled as expected after {time.monotonic() - t0:.2f}s: {e}")

            # ---- 2. cancel the whole session ----
            print("test 2: session.cancel()")
            futs = [slow.spawn(30) for _ in range(3)]

            def cancel_session_after(sec: float):
                time.sleep(sec)
                print(f"  client: session.cancel() after {sec}s")
                s.cancel()

            threading.Thread(target=cancel_session_after, args=(1.0,), daemon=True).start()

            t0 = time.monotonic()
            cancelled = 0
            for f in futs:
                try:
                    f.result(timeout=30)
                except TaskCancelled:
                    cancelled += 1
            print(f"  {cancelled}/{len(futs)} cancelled after {time.monotonic() - t0:.2f}s")


if __name__ == "__main__":
    main()
