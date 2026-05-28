"""Wrap an arbitrary callable (here a lambda) as a task by building the
``Task`` wrapper directly.

    uv run python examples/lambda_tasks.py --partition pymonikv1
"""

from __future__ import annotations

import argparse

from pymonik import PymonikClient, Task
import pymonik


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", default="pymonikv1")
    args = ap.parse_args()

    # Task(func, name=...) for anything you can't put @task on — lambdas,
    # third-party callables, partial()s. The name surfaces in worker logs.
    add = Task(lambda a, b: a + b, name="add_lambda")
    mul = Task(lambda a, b: a * b, name="mul_lambda")

    with PymonikClient() as client:
        with client.session(partition=args.partition):
            f1 = add.spawn(1, 2)
            f2 = mul.spawn(f1, 10)  
            print("(1 + 2) * 10 =", f2.result())


if __name__ == "__main__":
    main()
