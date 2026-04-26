# Observability

PymoniK emits OpenTelemetry spans for the whole task lifecycle:
session open, batch submission, blob upload, worker-side execution,
client-side waits. Spans propagate from client to worker via a W3C
trace context embedded in the task envelope, so a single trace covers
your submission *and* the work that ran on the cluster.

It's opt-in (no overhead when off) and the visualisation story is one
container.

## Install the optional dependency

```sh
uv add 'pymonik[otel]'
```

This pulls in `opentelemetry-api`, `opentelemetry-sdk`, and the OTLP
gRPC exporter. Without these installed, every OTel call site in
PymoniK is a no-op — the library runs unchanged.

## The minimum-infra visualisation

Run [Jaeger all-in-one](https://www.jaegertracing.io/docs/getting-started/)
locally — UI, OTLP collector, and storage in one container:

```sh
docker run --rm -d --name jaeger \
    -p 16686:16686 \
    -p 4317:4317 \
    -e COLLECTOR_OTLP_ENABLED=true \
    jaegertracing/all-in-one:latest
```

- `16686` — Jaeger UI ([http://localhost:16686](http://localhost:16686))
- `4317`  — OTLP/gRPC ingress

Point the client at it via standard OTel env vars:

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_SERVICE_NAME=pymonik-client      # optional; default "pymonik"
```

Run any pymonik script. Spans appear in Jaeger under the `pymonik`
service.

## Enabling tracing in code

PymoniK auto-detects when standard OTel env vars are set and turns on
tracing without further configuration. To force-enable / force-
disable from code:

```python
from pymonik import PymonikClient

with PymonikClient(otel=True) as client:    # force on, regardless of env
    ...

with PymonikClient(otel=False) as client:   # force off, even if env vars set
    ...
```

If your application already configures a `TracerProvider` (e.g. you
use OTel for other things), PymoniK detects and uses it — no second
provider, no double export.

## What spans you get

```
pymonik.session.open
└── pymonik.submit  (count=N, func=..., partition=...)
    ├── pymonik.task.run  [worker]  (task_id=..., attempt=1)
    ├── pymonik.task.run  [worker]
    └── ... (one per task in the batch)
pymonik.future.wait
pymonik.blob.upload
```

Span attributes you can filter on in the UI:

- `pymonik.func` — the decorated function name
- `pymonik.task_id` — the ArmoniK task id
- `pymonik.partition` — the partition the task was submitted to
- `pymonik.count` — batch size for `.map()` calls
- `pymonik.attempt` — 1 for fresh submissions, ≥2 for retries
- `pymonik.bytes` — size of an uploaded blob
- `pymonik.subprocess` / `pymonik.local` — task ran in subprocess
  (deps + isolate=True) or in LocalCluster

The submit span and the worker's `pymonik.task.run` span share a
trace id; the worker span's parent is the submit span. So in Jaeger
you click into one trace and see the whole batch.

## Workers in a real cluster

The local Jaeger container only sees client-side spans by default —
worker pods need to reach the same collector to export their spans.
Two ways:

1. **Bake the env vars into the worker image** when you build it (see
   [Worker images](worker-images.md)). The image's pod template gets
   `OTEL_EXPORTER_OTLP_ENDPOINT` baked in, pointing at an in-cluster
   collector reachable by both client and worker.
2. **Set the env at the partition level** via your ArmoniK Terraform
   variables (`workers[*].env`) so the polling agent injects the env
   var into worker pods at scale-up time.

For local-cluster setups (kind, Docker Desktop), `host.docker.internal:4317`
usually points to the host's Jaeger from within the cluster.

## End-to-end example

`examples/with_otel.py` ships with a runnable end-to-end demo against
LocalCluster. Run with the Jaeger container above:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
    uv run python examples/with_otel.py
```

Open Jaeger, find the trace, see the tree.

## Sampling and cost

By default head-sampling is 100% (every trace exported). For a
production cluster doing thousands of tasks per second, that's a lot
of span volume. Configure sampling via the standard OTel env vars:

```sh
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.01    # 1%
```

The client samples; if it decides to keep a trace, the W3C trace
context tells the worker to keep its span too. Sampling is consistent
end-to-end.

## What's not yet covered

ArmoniK itself (control plane, polling agent, agent sidecar) doesn't
emit OTel spans yet — that's an upstream item on their roadmap. Until
that lands, traces show a gap between the client's `pymonik.submit`
span and the worker's `pymonik.task.run` span: the polling agent's
wait time, queue depth, and dispatch latency happen there but don't
appear as spans. The trace tree is correct; it's just sparse in the
middle. Once ArmoniK ships native OTel, the W3C context PymoniK
already propagates feeds into their spans automatically — no PymoniK
changes needed.
