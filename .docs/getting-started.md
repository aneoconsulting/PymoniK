# Getting started

This page walks you from "no PymoniK installed" to "I just ran a
distributed computation on my cluster."

## Prerequisites

- An ArmoniK cluster you can talk to (any deploy: local quick-deploy,
  k8s, etc.) with a partition that runs a PymoniK-compatible worker
  image (see [Worker images](guides/worker-images.md)). The default
  partition name we'll use throughout is `pymonik`.
- [`uv`](https://docs.astral.sh/uv/) for project management.
- Python **3.11** locally — must match the worker's Python version
  (cloudpickle isn't cross-minor-compatible; see
  [Important considerations](important-considerations.md)).

## Install

```sh
mkdir hello_pymonik && cd hello_pymonik
uv init --python 3.11
uv add pymonik
```

## Point at your cluster

PymoniK reads cluster connection info from a YAML file the same way
the ArmoniK CLI does. Three precedence levels:

1. **Pass `endpoint=` (and `credentials=`) explicitly** to
   `PymonikClient(...)`.
2. **Pass `akconfig=/path/to/armonik-cli.yaml`** — loads the endpoint
   and (optionally) the CA / client cert / key for mTLS.
3. **Set `AKCONFIG=/path/to/armonik-cli.yaml`** in the environment.
   The constructor picks it up automatically.

Most users export `AKCONFIG` once and never pass anything to
`PymonikClient()` again:

```sh
export AKCONFIG=/path/to/generated/armonik-cli.yaml
```

## Hello, world

```python
from pymonik import PymonikClient, task

@task
def add(a: int, b: int) -> int:
    return a + b

# Local call still works exactly like a plain function. No client,
# no session, no setup — useful for tests, sanity checks, and
# debugging the function in isolation.
assert add(2, 3) == 5

with PymonikClient() as client:                      # reads $AKCONFIG
    with client.session(partition="pymonik") as s:
        result = add.spawn(2, 3).result(timeout=60)
        print(result)                                 # 5
```

What's happening:

- `@task` wraps `add` so it gets two callable shapes:
  - `add(2, 3)` — plain Python call, runs in the current process,
    returns `5`. The decorator doesn't change this.
  - `add.spawn(2, 3)` — remote submission, returns a `Future[int]`.
- `PymonikClient()` opens a gRPC channel; the `with` block closes it.
- `client.session(partition="pymonik")` creates an ArmoniK session
  bound to that partition. Tasks submitted inside its `with` block run
  there.
- `Future.result(timeout=60)` blocks until the result is delivered or
  the timeout fires. `await fut` is the async equivalent — see
  [Async](guides/async.md).

## Many tasks at once

```python
@task
def square(x: int) -> int:
    return x * x

@task
def add(a: int, b: int) -> int:
    return a + b

with PymonikClient() as client:
    with client.session(partition="pymonik") as s:
        squares = square.map(range(32))
        print(squares.results(timeout=120))                   # [0, 1, 4, 9, ...]

        sums = add.map(range(32), range(1, 33))
        print(sums.results(timeout=120))                      # [1, 3, 5, 7, ...]
```

`Task.map(*iterables)` mirrors Python's built-in `map`: it zips its
iterables (stopping at the shortest) and submits one task per zipped
tuple. The whole batch is one gRPC round-trip, not N. Returns a
`FutureList[T]`; use `.results(timeout=...)` to wait for everything in
submission order.

If you already have your arguments as tuples and just want each tuple
unpacked positionally, use `starmap` (the equivalent of
`itertools.starmap`):

```python
pairs = [(1, 2), (3, 4), (5, 6)]
sums = add.starmap(pairs)
```

`map` is the right shape for "I have parallel lists of inputs";
`starmap` is the right shape for "I already have a list of arg
tuples." Pick the one that doesn't make you build the wrong shape.

## Composing tasks (pipelining)

A `Future` (and a `FutureList`) is a first-class argument. Pass it to
another `.spawn()` and the SDK rewrites it as an ArmoniK data
dependency:

```python
@task
def add(a: int, b: int) -> int:
    return a + b

@task
def total(xs: list[int]) -> int:
    return sum(xs)

with PymonikClient() as client:
    with client.session(partition="pymonik") as s:
        partials = add.map(range(32), range(1, 33))   # FutureList[int]
        final = total.spawn(partials)                 # pass it directly
        print(final.result(timeout=120))
```

Two important properties:

- The client never blocks on `partials`. ArmoniK schedules `total` to
  run after every upstream `add` completes — the dependency edge is
  enough.
- `total` receives the *resolved* values as a plain `list[int]`. The
  SDK rewrites each upstream `Future` as a data dependency, downloads
  the result bytes on the worker, and substitutes them before calling
  your function. From the worker's perspective, it's just a list.

You can mix `Future` arguments with plain values freely — anything
that isn't a `Future` / `FutureList` / `Blob` / `Materialize` rides
inline (or auto-spills if it's too big; see
[Blobs and Materialize](guides/blobs-and-materialize.md)).

## Errors

Tasks that raise on the worker surface as `TaskFailed` on the client:

```python
from pymonik import TaskFailed

@task
def maybe_blow_up(x: int) -> int:
    if x < 0:
        raise ValueError("x must be non-negative")
    return x * 2

with PymonikClient() as client:
    with client.session(partition="pymonik") as s:
        try:
            maybe_blow_up.spawn(-1).result(timeout=30)
        except TaskFailed as e:
            print(e.task_id, e.worker_message)
```

Other typed exceptions in `pymonik`:

- `TaskCancelled` — task or session was cancelled.
- `TaskTimeout` — `.result(timeout=...)` exceeded.
- `NotInSessionError` — you called `.spawn()` outside a session block.
- `PymonikError` — base class; everything above derives from it.

Catch them with `try/except`. To **settle without raising** — branch on
success/failure rather than catch — use `fut.outcome()` for one task, or
`FutureList.outcomes()` for many (`gather(...)` returns a `FutureList`, so
`gather(...).outcomes()` works too). Each gives you an `Outcome` with `.ok`,
`.error`, and a lazily-downloaded `.value` (see [Async](guides/async.md)).

## Per-task options

`@task` accepts task-level overrides:

```python
from datetime import timedelta

@task(retries=3, partition="gpu", timeout=timedelta(minutes=5), priority=10)
def render(scene: bytes, frame: int) -> bytes:
    ...
```

The same options are settable per call via `.with_options(...)`
(returns a new bound task — never mutates the decorated function):

```python
fast_lane = render.with_options(partition="gpu-a100", priority=20)
fast_lane.spawn(scene, 0).result()
```

Merge order at submission time is **session default ← `@task(...)` ←
`.with_options(...)`**. `client.session(default_options=...)` lets you
set session-wide baselines:

```python
from pymonik import TaskOpts

with client.session(
    partition="pymonik",
    default_options=TaskOpts(retries=2, timeout=timedelta(seconds=30)),
) as s:
    ...
```

## Sub-tasking

A task can delegate its output to a child task using `task.tail(...)`:

```python
@task
def adaptive_add(a: list[int], b: list[int]) -> list[int]:
    if len(a) > 1024:
        mid = len(a) // 2
        return concat.tail(
            adaptive_add.spawn(a[:mid], b[:mid]),
            adaptive_add.spawn(a[mid:], b[mid:]),
        )
    return [x + y for x, y in zip(a, b)]
```

`tail()` returns a lazy promise; the framework binds it to the parent's
expected output id and submits the child task with that binding. The
child writes the parent's output directly. Use this for divide-and-
conquer; for fan-out / fan-in, plain `map` + `spawn` is simpler.

A task can also produce multiple named outputs via `MultiResult` —
downstream tasks then depend on individual fields, not the whole
result. See the [Sub-tasking and multi-output](guides/sub-tasking-and-multi-output.md)
guide.

## What's next

You now know enough to ship simple workloads. The guides cover
specific topics:

- [Runtime environment](guides/runtime-environment.md) — install pip
  packages on workers, set environment variables.
- [Blobs and Materialize](guides/blobs-and-materialize.md) — large
  arguments, file/directory materialisation.
- [Multi-partition routing](guides/multi-partition.md) — mix CPU and
  GPU partitions in one session.
- [Retries](guides/retries.md) — cluster-side vs client-side.
- [Local testing](guides/local-testing.md) — `LocalCluster` for unit
  tests.
- [Observability](guides/observability.md) — OTel + Jaeger, end-to-end.
- [Async](guides/async.md) — `await fut`, structured concurrency.
- [Worker images](guides/worker-images.md) — bake your project into a
  worker image for production.
