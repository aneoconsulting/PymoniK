"""Recursive subtasking + fan-in: adaptive vector addition.

Splits a vector in half until each chunk is under threshold, then
component-wise-adds the base case and concatenates up the tree. The
aggregation at each level is delegated to a sub-task so the parent's
expected output is fulfilled by the child (no intermediate hops).

Uses plain Python lists instead of numpy so the default worker image
doesn't need scipy/numpy baked in.

    uv run python examples/adaptive_vector_addition.py --partition pymonikv1
"""

from __future__ import annotations

import argparse

from pymonik import PymonikClient, current, task
import pymonik

CHUNK_THRESHOLD = 256


@task
def vec_add(a: list[int], b: list[int]) -> list[int]:
    """Recursive divide-and-conquer add. Delegates aggregation to a sub-task."""
    if len(a) != len(b):
        raise ValueError("vector length mismatch")

    if len(a) > CHUNK_THRESHOLD:
        current().log.info("splitting", size=len(a))
        mid = len(a) // 2
        left = vec_add.spawn(a[:mid], b[:mid])
        right = vec_add.spawn(a[mid:], b[mid:])
        # Delegate: the concat task's output *is* our output. When concat
        # completes, ArmoniK marks this task's result as ready too.
        return concat.tail(left, right)  # type: ignore[return-value]

    return [x + y for x, y in zip(a, b)]


@task
def concat(a: list[int], b: list[int]) -> list[int]:
    return a + b


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    ap.add_argument("--size", type=int, default=4096)
    args = ap.parse_args()

    vec_a = list(range(args.size))
    vec_b = [x * 2 for x in vec_a]
    expected = [a + b for a, b in zip(vec_a, vec_b)]

    with PymonikClient() as client:
        with client.session(partition=args.partition) as s:
            result = vec_add.spawn(vec_a, vec_b).result(timeout=300)
    if result == expected:
        print(f"adaptive add verified; size={args.size}, head={result[:6]} … tail={result[-6:]}")
    else:
        print(f"MISMATCH: got head={result[:6]} expected head={expected[:6]}")


if __name__ == "__main__":
    main()
