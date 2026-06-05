# Local testing

`LocalCluster` is a drop-in replacement for `PymonikClient` that runs
tasks in an in-process thread pool. The same `@task` definitions, the
same `.spawn()` / `.map()` / blob upload / `Future` API — no gRPC, no
cluster, no network.

It's the right tool for unit tests, fast iteration on task logic, and
CI lanes that exercise PymoniK without depending on a deployment.

## Quick start

```python
from pymonik import task
from pymonik.testing import LocalCluster

@task
def add(a: int, b: int) -> int:
    return a + b

def test_add():
    with LocalCluster() as client:
        with client.session() as s:
            assert add.spawn(2, 3).result(timeout=5) == 5
```

`LocalCluster()` opens a thread pool (default 16 threads); each
`.spawn()` enqueues the task; the pool runs it.

## What's exercised vs. what isn't

`LocalCluster` runs the **same submission pipeline** the real client
uses. Specifically:

- Arguments are walked for `Future` / `Blob` / `Materialize` and
  rewritten into wire refs (`extract_deps`).
- Auto-spill kicks in for oversize args.
- The envelope is encoded with msgspec.
- A worker thread decodes the envelope, resolves data dependencies
  from a session-local dict, runs the function, and pickles the
  result.

So bugs in envelope encoding, ref resolution, blob auto-spill, or
runtime-deps env management surface here the same way they would on
the cluster.

What's local-only:

- No pod scheduling latency, no partition routing, no autoscaling.
- No worker isolation — every task runs in the host process.
- ArmoniK's cluster-side `max_retries` (infra-failure retries) isn't
  emulated. Client-side `@task(retry_on=...)` retries *do* run end-to-
  end through the same code path the real session uses.

## Async

```python
import pytest
import anyio

@pytest.mark.anyio
async def test_add_async():
    async with LocalCluster() as client:
        async with client.session_async() as s:
            assert await add.spawn(2, 3) == 5
```

Both asyncio and trio backends work via `pytest-anyio`.

## Runtime deps in tests

```python
@task(deps=["numpy"])
def numpy_sum(n: int) -> int:
    import numpy as np
    return int(np.arange(n).sum())

def test_numpy_dep(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    with LocalCluster() as client:
        with client.session() as s:
            assert numpy_sum.spawn(100).result(timeout=600) == 4950
```

`LocalCluster` exercises the *real* env builder — `uv` runs, a venv
is built at `PYMONIK_ENVS_ROOT/<env_id>/.venv`, the splice (or
subprocess) path runs identically to the worker. The first call pays
the install; subsequent ones reuse the env.

`PYMONIK_ENVS_ROOT` defaults to `~/.cache/pymonik/envs`; override per
test so runs don't pollute each other.

## Multi-task pool sizing

Default is 16 worker threads. A pipeline with deeper in-flight depth
can deadlock — every thread parks waiting for an upstream that needs
a thread to compute. Increase the pool for deep DAGs:

```python
LocalCluster(max_workers=64)
```

(The deadlock is a known limitation of the in-process dispatcher;
a future anyio refactor will drop it.)

## Cache and OTel

Both work the same as on the real client:

```python
LocalCluster(cache=True)                 # exec cache enabled
# OTEL_EXPORTER_OTLP_ENDPOINT=... exports spans normally
```

See [Observability](observability.md).

## When LocalCluster isn't enough

Two cases need a real cluster:

- **Cluster-side `max_retries`** — only ArmoniK enforces infra
  retries.
- **Things that depend on the polling agent** — partition queue
  depth, pod scheduling latency, image pull behaviour.

For those, use `pytest -m e2e` and a `testcontainers`-spun ArmoniK or
a dev-deploy. Most behavioural tests don't need either.
