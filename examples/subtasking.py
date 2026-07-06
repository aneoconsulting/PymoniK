"""Sub-tasking via ``task.tail()``.

Decreasing-counter recursion: each task either terminates or spawns a
single child with the parent's expected output slot. ArmoniK stitches the
chain together; the client only sees the final answer.

    uv run python examples/subtasking.py --partition pymonikv1 --depth 5
"""

from __future__ import annotations

import argparse

from pymonik import PymonikClient, current, task
import pymonik


@task
def increment_chain(n: int, acc: int) -> int:
    """Base case returns acc; recursive step delegates its output to a child."""
    current().log.info("step", n=n, acc=acc)
    if n <= 0:
        return acc
    # `tail()` submits the child with this task's expected_output_id, so
    # ArmoniK marks our output as done when the child completes. The
    # returned TailPromise tells the dispatcher we handed off.
    return increment_chain.tail(n - 1, acc + 1)  # type: ignore[return-value]


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    ap.add_argument("--depth", type=int, default=5)
    args = ap.parse_args()

    with PymonikClient() as client:
        with client.session(partition=args.partition) as s:
            result = increment_chain.spawn(args.depth, 0).result(timeout=300)
    print(f"chain({args.depth}) -> {result}  (expected {args.depth})")


if __name__ == "__main__":
    main()
