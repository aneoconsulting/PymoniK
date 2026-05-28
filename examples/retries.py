"""Decorator-level retries with exception-type filter and backoff.

Shows two flavours:

1. Cluster-side blanket retries via ``@task(retries=N)`` — ArmoniK retries
   N times for any failure (infra or user-code).

2. Client-side filtered retries via ``@task(retries=N, retry_on=(...,),
   retry_backoff=...)`` — the SDK observes the failure type, sleeps the
   configured backoff, and re-spawns. Only matching exceptions retry;
   anything else surfaces immediately.

The ``flaky`` task fails its first 2 attempts and then succeeds, so we
can see the retry loop without flapping.

    uv run python examples/retries.py --partition pymonikv1
"""

from __future__ import annotations

import argparse
import time

from pymonik import PymonikClient, TaskFailed, current, task
import pymonik


# ---- a deterministic flaky function ----
# Uses ``pymonik.current().attempt`` (threaded through the envelope on
# every retry) so the function's success condition is independent of where
# it lands — works the same on the cluster and under LocalCluster.
@task(retries=4, retry_on=(TaskFailed,), retry_backoff="exponential")
def flaky_with_filter(label: str) -> str:
    ctx = current()
    n = ctx.attempt
    ctx.log.info("flaky attempt", label=label, attempt=n)
    if n < 3:
        raise RuntimeError(f"intentional failure on attempt {n}")
    return f"{label} succeeded on attempt {n}"


# ---- a never-retried failure shape ----
# `retry_on=(KeyError,)` means a RuntimeError will surface immediately
# without retrying — we expect this to fail on the first attempt.
@task(retries=4, retry_on=(KeyError,), retry_backoff="constant")
def fails_with_unmatched_type(label: str) -> str:
    raise RuntimeError(f"{label}: deliberately unmatched failure")


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    with PymonikClient() as client:
        with client.session(partition=args.partition) as s:
            # 1) Filtered retries actually retry until success.
            t0 = time.monotonic()
            label = f"run-{int(time.time())}"
            try:
                result = flaky_with_filter.spawn(label).result(timeout=120)
                print(f"flaky_with_filter: {result}  ({time.monotonic() - t0:.1f}s)")
            except Exception as e:
                print(f"flaky_with_filter: exhausted -> {e}")

            # 2) Unmatched exception type — no retry, raises immediately.
            t0 = time.monotonic()
            try:
                fails_with_unmatched_type.spawn("immediate").result(timeout=60)
                print("UNEXPECTED success on fails_with_unmatched_type")
            except TaskFailed as e:
                print(
                    f"fails_with_unmatched_type: surfaced after "
                    f"{time.monotonic() - t0:.1f}s as expected; head={str(e)[:80]}…"
                )


if __name__ == "__main__":
    main()
