"""Minimal smoke test: submit a single task and print its result.

Easiest:

    export AKCONFIG=/path/to/generated/armonik-cli.yaml
    uv run python examples/hello.py

Or explicit:

    uv run python examples/hello.py --endpoint <host:port> --partition pymonik
"""

from __future__ import annotations

import argparse

from pymonik import PymonikClient, task
import pymonik


@task
def add(a: int, b: int) -> int:
    return a + b


@task
def greet(name: str) -> str:
    return f"hello, {name}, from the ArmoniK worker"


def main() -> None:
    pymonik.enable_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=None, help="overrides AKCONFIG if given")
    ap.add_argument("--partition", default="pymonik")
    args = ap.parse_args()

    with PymonikClient(endpoint=args.endpoint) as client:
        with client.session(partition=args.partition) as s:
            f1 = add.spawn(2, 3)
            f2 = greet.spawn("pymonik")
            print("submitted; waiting for results")

            print("add(2, 3) ->", f1.result(timeout=120))
            print("greet('pymonik') ->", f2.result(timeout=120))


if __name__ == "__main__":
    main()
