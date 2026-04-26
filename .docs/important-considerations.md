# Important considerations

A small number of constraints don't show up in the API but bite if you
don't know about them. Read this once before shipping.

## Python version pinning

**The client's Python minor version must match the worker's.** PymoniK
ships your function as a `cloudpickle` blob; cloudpickle bytecode is
not cross-minor-compatible. A 3.11 client against a 3.12 worker will
SIGSEGV during unpickle in the worker process — usually with no
traceback, just a non-zero exit.

PymoniK's wire envelope embeds `sys.version_info` and the worker
rejects mismatches with a typed `ValueError`, but the cleaner fix is
to pin your client's Python to whatever the worker image was built
with. The default worker image is built for Python 3.11; if your
project uses 3.11 too, you're set. To run a different version you'll
need to bake a matching worker image (see
[Worker images](guides/worker-images.md)).

In `pyproject.toml`:

```toml
[project]
requires-python = "==3.11.*"
```

## Cloudpickle and multi-file projects

Single-file scripts work out of the box: cloudpickle pickles
functions in `__main__` *by value* — bytecode + globals — so the
worker doesn't need the source on disk.

Multi-file projects don't. cloudpickle pickles functions in normal
modules *by reference*: it stores `(module_name, qualname)` and the
worker re-imports `module_name` to look the function up. If the
worker doesn't have your project installed, the import fails and the
task does too.

Two answers, both supported:

1. **Bake your project into the worker image.** The recommended
   production path — once the image has `pip install .` of your code,
   every task can find every helper. See
   [Worker images](guides/worker-images.md).
2. **Tell cloudpickle to pickle your package by value too.** At your
   client's entrypoint:

   ```python
   import cloudpickle
   import mypkg

   cloudpickle.register_pickle_by_value(mypkg)
   ```

   Now functions in `mypkg.tasks`, `mypkg.utils`, etc. are pickled the
   same way `__main__` functions are. The worker doesn't need
   `mypkg` installed — it reconstructs from the pickled bytes.

The first is right for production; the second is right for fast
iteration without rebuilding the image on every change.

A future `additional_modules=` option will automate (2). For now,
`register_pickle_by_value` is the primitive.

## Partitions and routing

A session is bound to one or more partitions on the cluster. By
default `client.session(partition="pymonik")` allows only that
partition; if a task tries to route to anything else (`@task(partition="gpu")`),
submission is rejected at the client.

To allow a task to choose, declare the set up front:

```python
with client.session(partition=["cpu", "gpu"]) as s:
    fast = render.with_options(partition="gpu").spawn(scene)
```

The first partition in the list is the default for tasks that don't
specify one. See [Multi-partition routing](guides/multi-partition.md).

## Result delivery: events vs polling

By default the client opens a server-streamed gRPC `Events.GetEvents`
call to receive completions. Latency from "result ready" to "future
resolved" is a few ms.

If the events stream misbehaves in your environment (proxies, network
policies that mangle long-lived streams), fall back to polling:

```python
PymonikClient(events=False, polling_interval=1.0, polling_chunk=200)
```

Polling does one `Tasks.list_results` RPC every `polling_interval`
seconds, batched into chunks of `polling_chunk` ids. Higher latency,
no streaming connection.

## Argument size and auto-spill

Anything you pass to `.spawn()` rides in the task's payload — except
when the cloudpickled bytes exceed `spill_threshold` (default 256 KiB,
configurable on the client). Large args are uploaded as blobs and
referenced via `data_dependencies` automatically; you don't need to
think about it.

If you're passing the same big object to many tasks, upload it once
explicitly:

```python
import pymonik.blob as blob

shared = blob.upload(big_dict)  # uploaded once
for i in range(1000):
    process.spawn(shared, i)    # all 1000 tasks share the same blob_id
```

See [Blobs and Materialize](guides/blobs-and-materialize.md).

## Worker-side blocking is illegal

Inside a `@task` body, `Future.result()` / `await future` raises:

```python
@task
def parent() -> int:
    child = other.spawn(...)
    return child.result()   # PymonikError — workers don't poll for results
```

ArmoniK tasks are ephemeral; blocking inside one ties up a pod
indefinitely. Pass the future to another `.spawn()` (creates a data
dependency edge so ArmoniK runs the next task once this one
completes), or return it with `_delegate=True` to hand off your
expected output. See [Sub-tasking in Getting Started](getting-started.md#sub-tasking).

## Returning multiple results

A task returns one Python object. If you `return a, b, c`, the worker
pickles the tuple and downstream tasks receive the tuple. There's no
way today to declare "this task produces three independent outputs."
If you need that, return a dict and have downstream tasks pick keys,
or split into three tasks.

## Cancellation propagation

`session.cancel()` and `future.cancel()` issue ArmoniK
`CancelTasks`/`CancelSession` RPCs and resolve pending futures locally
with `TaskCancelled`. Worker-side cancellation observation
(`pymonik.current().cancel_if_requested()`) requires a small
upstream-armonik change that's not yet merged, so for now the worker
only learns about cancellation when its gRPC channel is torn down.

## Logging

The library is **silent by default** (uses a `NullHandler`). To see
PymoniK's structured logs:

```python
import pymonik
pymonik.enable_logging("INFO")
```

Workers always log — operators rely on the polling-agent → k8s
pipeline to surface what each pod is doing. Set
`PYMONIK_WORKER_LOG_LEVEL` in the worker environment to override.
