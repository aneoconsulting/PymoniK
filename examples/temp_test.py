# TODO: Remove this later
from __future__ import annotations

import argparse
import time

from pymonik import PymonikClient, task
import pymonik


@task
def add(a: int, b: int) -> int:
    return a + b



def main() -> None:
    pymonik.enable_logging()
    with PymonikClient() as client:
        with client.session(partition="pymonikv1") as s:
            seed = add.spawn(2, 3)

            print("seed ->", seed.result())

if __name__ == "__main__":
    main()
