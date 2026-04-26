# Introduction

PymoniK is a Python framework for writing distributed programs that run
on an [ArmoniK](https://github.com/aneoconsulting/ArmoniK) cluster. It
sits on top of the lower-level `armonik` Python client and gives you a
decorator-first API that feels like calling regular functions.

## What it gives you

**A decorator turns any function into a remote task.**

```python
from pymonik import task

@task
def hello() -> str:
    return "hello world"
```

Inside a session, `hello.spawn()` submits the function for remote
execution and returns a `Future[str]`. `hello()` still calls the
function locally — the decoration doesn't get in your way during
debugging.

**Many tasks at once, batched into one round-trip.**

```python
@task
def add(a: int, b: int) -> int:
    return a + b

results = add.map(range(32), range(1, 33))
```

`map` zips its iterables (Python-stdlib semantics) and packs all 32
submissions into a single gRPC call. Returns a `FutureList[int]`.

**Pipelines compose by passing futures as arguments.**

```python
@task
def total(xs: list[int]) -> int:
    return sum(xs)

partials = add.map(range(32), range(1, 33))
final = total.spawn(partials)
print(final.result(timeout=60))
```

`final` doesn't wait for `partials` on the client. PymoniK rewrites
each `Future` into an ArmoniK data dependency edge. The cluster runs
`total` as soon as the upstream `add` tasks complete; the client only
blocks on the terminal `result()`. `total`'s function body receives a
plain `list[int]` — the SDK resolves the futures on the worker before
calling.

**Local execution is a flag away.**

```python
from pymonik.testing import LocalCluster

with LocalCluster() as client:
    with client.session() as s:
        assert add.spawn(2, 3).result(timeout=5) == 5
```

`LocalCluster` is a drop-in for `PymonikClient` that runs tasks in a
thread pool. Same envelope encoding, same dispatch pipeline — pytest
without a cluster.

## Where it fits

PymoniK is the highest-level Python SDK for ArmoniK. Underneath, it
uses the official `armonik` Python client for control-plane RPCs and
the standard worker framework for the agent sidecar. Anything you can
do with the lower-level SDK (filters, sessions, multi-partition,
priorities, retries) is reachable from PymoniK without dropping down.

The library is opinionated about ergonomics — `Future[T]` over result
handles, decorators over registries, structured exceptions over raw
gRPC errors — but it doesn't hide ArmoniK from you. When you need a
filter query, the polling agent's cache, or the partition catalogue,
they're a property access away (`client.tasks`, `client.partitions`,
`client.results`, etc.).

## Where to go next

- [Getting started](getting-started.md) — install, configure, run your
  first task.
- [Important considerations](important-considerations.md) — the small
  number of constraints that bite if you don't know about them
  (Python version pinning, cloudpickle minor compatibility, multi-file
  project shipping).
- The guides cover specific topics: runtime dependencies, blobs and
  file materialisation, multi-partition routing, retries, local
  testing, observability, async usage, and worker image building.
