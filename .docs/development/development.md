# Developing PymoniK

This page covers what you need to know to work *on* PymoniK itself —
not on top of it.

## Prerequisites

- Python 3.11. PymoniK pins to 3.11 because cloudpickle isn't
  cross-minor-compatible with the worker image — tests and
  `LocalCluster` need to match what the worker runs.
- [`uv`](https://docs.astral.sh/uv/) for project management.
- Docker, if you'll touch worker images or run integration tests
  against a real cluster.

## Layout

```
src/pymonik/                 # the package
  __init__.py                #   public API re-exports
  client.py                  #   PymonikClient
  session.py                 #   Session + completion loops
  task.py                    #   @task decorator, Task wrapper
  future.py                  #   Future, FutureList
  options.py                 #   TaskOpts, merge semantics
  envelope.py                #   wire format (msgspec)
  blob.py                    #   Blob, Materialize
  worker.py                  #   pymonik-worker entrypoint
  worker_session.py          #   from-inside-a-worker submission
  context.py                 #   pymonik.current() / WorkerContext
  errors.py                  #   PymonikError hierarchy
  composition.py             #   gather, as_completed
  testing/                   #   LocalCluster
  cli/                       #   pymonik CLI (stub today)
  _internal/                 #   not part of the public API
    submit.py                #     shared submission pipeline
    refs.py                  #     FutureRef / BlobRef / MaterializeRef
    env_builder.py           #     uv venv + flock for runtime deps
    subprocess_dispatch.py   #     deps + isolate=True path
    task_runner.py           #     subprocess child entrypoint
    exec_cache.py            #     local result cache
    query.py                 #     fluent introspection
    info.py                  #     TaskInfo / ResultInfo / ...
    channel.py               #     gRPC channel helpers
    _otel.py                 #     OpenTelemetry helper
    _logging.py              #     opt-in structlog setup
examples/                     # runnable examples (also CI-gated)
tests/                        # pytest suite
.docs/                        # this documentation (Sphinx + MyST)
worker-image/                 # Dockerfile for the harmonic_snake worker
```

The `_internal/` prefix marks code that may change without notice.
Anything re-exported from `pymonik/__init__.py` is part of the public
API and follows semver-ish rules within the alpha.

## Running tests

```sh
uv sync                      # one-time install
uv run pytest                # everything
uv run pytest -m "not slow"  # skip slow integration tests (no `uv` install)
uv run pytest tests/test_otel.py -v   # one file
```

The test suite is divided:

- **Fast tests** (~30) — pure unit tests, no network, no `uv venv`
  builds. Run in seconds. These are what CI runs on every push.
- **Slow tests** marked `@pytest.mark.slow` — exercise the runtime
  deps path with a real `uv` install. Need `uv` on `PATH`. Skip on
  Windows (the subprocess wire is POSIX-only for now).

The `LocalCluster` exercises the same submission pipeline as the
real client, so most behavioural tests don't need a cluster. Only
tests that depend on cluster-side behaviour (partition scheduling,
events stream over the network) need a live ArmoniK; mark those
`@pytest.mark.e2e` and run them separately when you have a deploy.

## Type checking

```sh
uv run ty check src/pymonik
```

New code should be fully annotated; private helpers may skip
annotations when obvious.

A few upstream-typing quirks (anyio's `to_thread.run_sync` overload
resolution, armonik's `Result` field types) produce false positives
in `Session` / `WorkerSession` / `task.py`. These predate the
revamp and aren't from new changes — leave them be unless you're
fixing them upstream.

## Linting and formatting

```sh
uv run ruff check
uv run ruff format
```

Ruff replaces black + flake8 + isort. Configuration is in
`pyproject.toml` under `[tool.ruff]`.

## Working against a cluster

If you're touching code that affects the worker (anything in
`worker.py` or `_internal/`), you'll need to rebuild the image and
restart the partition:

```sh
docker build -t my-org/harmonic_snake:dev worker-image/
docker push my-org/harmonic_snake:dev    # or load directly into your kind cluster
kubectl rollout restart deployment/compute-plane-pymonik -n armonik
```

For client-only changes, just `uv sync` (or run with the editable
install) and re-run your client script — no image rebuild needed.
