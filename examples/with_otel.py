"""OTel tracing — opt-in, end-to-end, with a one-container UI.

The minimum-infra path for visualising spans:

1. Run Jaeger all-in-one (UI + OTLP collector + storage in one container)::

       docker run --rm -d --name jaeger \\
           -p 16686:16686 \\
           -p 4317:4317 \\
           -e COLLECTOR_OTLP_ENABLED=true \\
           jaegertracing/all-in-one:latest

   - 16686 — Jaeger UI (http://localhost:16686)
   - 4317  — OTLP/gRPC ingress (where pymonik exports to)

2. Point both client and worker at it via env vars::

       export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
       export OTEL_SERVICE_NAME=pymonik-client          # optional
       export OTEL_EXPORTER_OTLP_INSECURE=true          # for plain http

   For a real ArmoniK cluster, set the same vars on the worker pods (bake
   into the worker image or set in the partition's pod template). For
   ``LocalCluster``, exporting on the client side is enough — the worker
   ``LocalSession`` runs in the same process.

3. Run this script::

       uv pip install 'pymonik[otel]'
       OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \\
           uv run python examples/with_otel.py

4. Open http://localhost:16686, pick "pymonik" service, hit "Find traces".

You should see a tree like::

    pymonik.session.open
    └── pymonik.submit  (count=8, func=square)
        ├── pymonik.task.run  (task_id=..., attempt=1)
        ├── pymonik.task.run
        └── ... (8 leaves)
    pymonik.future.wait
"""

from __future__ import annotations

import time

import pymonik
from pymonik import task
from pymonik.testing import LocalCluster


@task
def square(x: int) -> int:
    return x * x


@task
def total(xs: list[int]) -> int:
    return sum(xs)


def main() -> None:
    pymonik.enable_logging()

    # otel=None auto-detects from OTEL_EXPORTER_OTLP_ENDPOINT etc. Pass
    # otel=True to force-enable (handy if you've configured a TracerProvider
    # yourself and just want pymonik to emit spans).
    with LocalCluster() as client:
        with client.session(partition="local") as s:
            t0 = time.monotonic()
            partials = square.map(range(8))
            answer = total.spawn(partials).result(timeout=30)
            elapsed = time.monotonic() - t0
            print(f"sum of squares 0..7 = {answer}  (elapsed {elapsed:.2f}s)")


if __name__ == "__main__":
    main()
