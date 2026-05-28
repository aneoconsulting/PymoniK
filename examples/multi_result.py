"""Multi-output tasks via ``MultiResult``.

A ``@task`` body can return ``MultiResult(a=..., b=...)`` to produce
multiple named outputs in ArmoniK. The decorator walks the function's
AST at decoration time to learn the field set, so the framework can
allocate one ``expected_output_id`` per field. Two properties follow:

- Downstream tasks can depend on a single field; fast fields don't
  gate slow ones.
- Any field's value can be a :class:`TailPromise` (from ``task.tail``),
  delegating that one slot to a child task that writes the result
  directly to the field's ``result_id`` — no relay through the parent.

This script demonstrates:

1. Basic spawn shape: per-field ``Future`` access on the handle, and
   the whole-task ``.result()`` returning a ``MultiResultView``.
2. Per-field downstream dependency.
3. Per-field tail-call: one slot produced by a delegated child task.
4. Collective ``.wait()`` / ``.done``.

    uv run python examples/multi_result.py --partition <pymonik-partition>
"""

from __future__ import annotations

import argparse
import time

from pymonik import MultiResult, PymonikClient, task
import pymonik


@task
def stats(xs: list[int]) -> MultiResult:
    """Return four named outputs; each becomes its own ``result_id``."""
    n = len(xs)
    s = sum(xs)
    mean = s / n
    var = sum((x - mean) ** 2 for x in xs) / n
    return MultiResult(count=n, total=s, mean=mean, var=var)


@task
def format_mean(m: float) -> str:
    return f"mean={m:.3f}"


@task
def slow_double(x: float) -> float:
    """Deliberately slow child for the tail-call demo."""

    time.sleep(2.0)
    return x * 2.0


@task
def split_with_tail(x: float) -> MultiResult:
    """``half`` is produced locally; ``doubled`` is delegated to a child.

    The child writes directly to the ``doubled`` slot's ``result_id``.
    The parent task is done as soon as it returns; ``doubled`` stays
    pending until ``slow_double`` finishes.
    """
    return MultiResult(
        half=x / 2.0,
        doubled=slow_double.tail(x),
    )


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    with PymonikClient() as client:
        with client.session(partition=args.partition) as s:
            # ---- 1. Handle shape ----
            out = stats.spawn([1, 2, 3, 4, 5, 6, 7, 8])
            print(f"handle: {out!r}")
            print(f"fields available: {out.fields}")

            # Each field is its own Future — attribute or item access.
            print(f"  count -> {out.count.result(timeout=120)}")
            print(f"  mean  -> {out['mean'].result(timeout=120):.3f}")

            # Whole-task ``.result()`` returns a MultiResultView (attr + dict).
            view = out.result()
            print(f"  view.var       = {view.var:.3f}")
            print(f"  dict(view)     = {dict(view)}")
            print(f"  view == dict?  {view == dict(view)}")

            # ---- 2. Per-field downstream dependency ----
            # ``format_mean`` consumes only ``mean``; ArmoniK keeps it
            # PENDING via data_dependencies on just that one result_id.
            t0 = time.monotonic()
            out2 = stats.spawn([10, 20, 30, 40])
            msg = format_mean.spawn(out2.mean).result(timeout=120)
            print(
                f"downstream-from-single-field: {msg}  "
                f"({time.monotonic() - t0:.1f}s)"
            )

            # ---- 3. Per-field tail-call ----
            # ``half`` resolves as soon as ``split_with_tail`` returns;
            # ``doubled`` stays pending until ``slow_double`` completes.
            t0 = time.monotonic()
            tailed = split_with_tail.spawn(5.0)
            print(
                f"half     -> {tailed.half.result(timeout=120)}  "
                f"({time.monotonic() - t0:.1f}s)"
            )
            print(
                f"doubled  -> {tailed.doubled.result(timeout=120)}  "
                f"({time.monotonic() - t0:.1f}s)"
            )

            # ---- 4. Collective wait / done ----
            handle = stats.spawn([5, 10])
            handle.wait()
            print(f"all done? {handle.done} -> {dict(handle.result())}")


if __name__ == "__main__":
    main()
