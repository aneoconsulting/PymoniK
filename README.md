<div align="center">

# PymoniK

[![Publish Docker images](https://github.com/aneoconsulting/PymoniK/actions/workflows/publish-images.yml/badge.svg?branch=main&event=release)](https://github.com/aneoconsulting/PymoniK/actions/workflows/publish-images.yml)
![GitHub Release](https://img.shields.io/github/v/release/aneoconsulting/PymoniK)

A dead-simple Python SDK for [ArmoniK](https://github.com/aneoconsulting/ArmoniK).

< [Documentation](https://pymonik.readthedocs.io/en/latest) | [Getting Started](https://pymonik.readthedocs.io/en/latest/getting-started.html) | [Contributing](https://pymonik.readthedocs.io/en/latest/development/contribution.html) >

</div>

## Quick start

```python
from pymonik import PymonikClient, task

@task
def add(a: int, b: int) -> int:
    return a + b

@task
def sum_all(xs: list[int]) -> int:
    return sum(xs)

with PymonikClient() as client:                     # reads $AKCONFIG
    with client.session(partition="pymonik") as s:
        # Pipelining: pass futures as args. No client-side blocking — ArmoniK
        # chains the tasks via data_dependencies. Only the terminal .result()
        # actually waits.
        parts = add.map(range(16), range(1, 17))
        total = sum_all.spawn(parts)
        print(total.result())
```

`Task.map(*iterables)` zips its iterables and submits one task per
zipped tuple — exactly Python's built-in `map` shape. If you already
have arg tuples, use `Task.starmap(args_iter)` instead.

Async too:

```python
import asyncio
from pymonik import PymonikClient, gather, task

@task
def double(x: int) -> int:
    return x * 2

async def main():
    async with PymonikClient() as client:
        async with client.session_async(partition="pymonik") as s:
            futures = double.map(range(8))
            results = await gather(futures)
            print(results)

asyncio.run(main())
```

Multiple named outputs from one task — downstream consumers depend on
fields, not the whole result, so a slow field doesn't gate the others:

```python
from pymonik import MultiResult, task

@task
def split(x: int):
    return MultiResult(double=x * 2, triple=x * 3)

with PymonikClient() as client:
    with client.session(partition="pymonik") as s:
        out = split.spawn(7)
        print(out.double.result(), out.triple.result())   # 14 21
        print(out.result()) # {double: 14, triple: 21}
```

Sub-tasking: a `@task` body can delegate its output to another task
via `task.tail(...)` — the parent's expected output is fulfilled by
the child, no intermediate hops:

```python
@task
def adaptive(n: int) -> int:
    if n < 1024:
        return base.tail(n)        # base writes our output directly
    return n
```

No cluster handy? Run the same code in-process with `LocalCluster`:

```python
from pymonik import task
from pymonik.testing import LocalCluster

@task
def add(a, b): return a + b

with LocalCluster() as client:
    with client.session() as s:
        assert add.spawn(2, 3).result() == 5
```

## Layout

```
src/pymonik/             Python package
  _internal/             implementation details (submit pipeline, refs, cache)
  cli/                   `pymonik` CLI (click)
  testing/               LocalCluster / LocalSession
worker-image/            Dockerfile baking the worker entrypoint
examples/                Live, runnable examples
.docs/                   Sphinx documentation (Sphinx + MyST)
tests/                   pytest suite (unit + slow integration via LocalCluster)
```

## Requirements

- Python ≥ 3.11 and < 3.13 (cloudpickle is not cross-minor; the worker
  image's Python must match the client's).
- An ArmoniK cluster — see the [ArmoniK getting-started guide](https://armonik.readthedocs.io/en/latest/content/armonik/getting-started.html).
- For local-only tests: nothing else; `LocalCluster` runs in-process.
