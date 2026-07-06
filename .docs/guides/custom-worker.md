# Custom worker entrypoints

The `pymonik-worker` console script is enough for almost everyone:
it runs the dispatch loop, decodes task envelopes, calls your
`@task` functions, and ships results back. But sometimes you want to
do something *before* PymoniK takes over the process — emit a metric,
configure logging, monkey-patch a library, set up a tracer.

## Wrapping the dispatcher

Write a tiny Python entrypoint that calls `pymonik.worker.run()`
yourself:

```python
# my_worker.py
import logging
import os

import pymonik
from pymonik.worker import run


def main() -> None:
    pymonik.enable_logging(level=os.getenv("LOG_LEVEL", "INFO"))
    logging.getLogger("my_app").setLevel(logging.DEBUG)

    # Any one-time setup the worker process needs.
    _configure_internal_metrics()
    _patch_third_party_lib()

    run()                         # blocks until ArmoniK tears the pod down


if __name__ == "__main__":
    main()
```

Then point your image's `ENTRYPOINT` at it:

```dockerfile
COPY --chown=armonikuser:armonikuser my_worker.py /app/
ENTRYPOINT ["python", "/app/my_worker.py"]
```

`pymonik.worker.run()` does exactly what the `pymonik-worker`
console script does. It:

1. Calls `pymonik.enable_logging(level=$PYMONIK_WORKER_LOG_LEVEL)`.
2. Sets up OTel if `OTEL_*` env vars are present.
3. Patches the upstream worker class to route the gRPC context to
   `WorkerContext.cancel_if_requested()`.
4. Hands control to the upstream `armonik_worker()` framework, which
   serves tasks until the pod is killed.

## Inside a task: WorkerContext

User code running inside a `@task` function can reach a worker-side
context via `pymonik.current()`:

```python
import pymonik
from pymonik import task

@task
def long_running(x: int) -> int:
    ctx = pymonik.current()

    ctx.log.info("starting", input=x, attempt=ctx.attempt)

    for i in range(x):
        ctx.cancel_if_requested()    # raises TaskCancelled if cluster cancelled
        # ... work ...

    return x * 2
```

`WorkerContext` exposes:

- `task_id`, `session_id` — for logs and external IDs.
- `attempt` — 1 for the original submission, 2+ for retries. Useful
  for idempotency-aware code that needs to know "this is a re-run."
- `log` — a `structlog`-style logger pre-bound with `task_id` /
  `session_id`.
- `cancel_if_requested()` — polls whether the gRPC server context is
  still active. Raises `TaskCancelled` if not.

## Don't override the dispatcher

The temptation is to write your own task processor — read the
envelope, call user code, ship the result. **Don't.** PymoniK's
dispatcher handles a lot of the wire format that's easy to get
wrong:

- msgspec envelope decoding with version checks.
- cloudpickle minor-version validation.
- `Future` / `Blob` / `Materialize` argument resolution.
- `data_dependencies` substitution.
- Sub-task delegate handling.
- Subprocess vs splice routing for `deps=`.
- OTel context extraction and span wrapping.
- Cooperative cancellation observation.

If you want to extend the worker, do it *around* `run()` (logging,
metrics, OTel) — not in place of it.

## Image hygiene

Whatever your entrypoint, the image still needs:

- A non-root `armonikuser` (uid 5000) owning `/app` and `/cache`.
- `pymonik` importable in the runtime venv.
- The Python minor matching the client's.

See [Worker images](worker-images.md) for the full Dockerfile.
