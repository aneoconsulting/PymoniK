# Worker images

A PymoniK worker pod runs the `pymonik-worker` console script
inside a Docker image. The image needs:

- A Python interpreter matching the client's minor version.
- The `pymonik` package installed.
- Whatever Python libraries (and your project code) the tasks import.
- A non-root `armonikuser` and a writable `/cache` directory.

The official base image (`dockerhubaneo/harmonic_snake`) gives you
the first three; you bake on top of it for project-specific deps.

## Choosing the right image

| Situation | What to do |
|-----------|------------|
| You're prototyping, no project code on workers, all deps fit `pymonik[deps]=...` | Use the base image as-is, declare deps via `client.session(deps=[...])`. See [Runtime environment](runtime-environment.md). |
| You have a multi-file project and want fast iteration | Base image, run `cloudpickle.register_pickle_by_value(mypkg)` at your client's entry. See [Important considerations](../important-considerations.md#cloudpickle-and-multi-file-projects). |
| You're shipping production: pinned deps, your project code, no install cost on every pod scale-out | Bake your own image. |

## Adding the worker partition to your cluster

The PymoniK worker runs as a partition in your ArmoniK Terraform
config:

```hcl
pymonik = {
  replicas = 0   # scale-from-zero; HPA below brings up pods on demand
  polling_agent = {
    limits   = { cpu = "2000m", memory = "2048Mi" }
    requests = { cpu = "50m",   memory = "50Mi"   }
  }
  worker = [
    {
      image = "dockerhubaneo/harmonic_snake"
      tag   = "python-3.11-2.0.0a3"   # MATCH your client's Python + pymonik version
      limits   = { cpu = "1000m", memory = "1024Mi" }
      requests = { cpu = "50m",   memory = "50Mi"   }
    }
  ]
  hpa = {
    type              = "prometheus"
    polling_interval  = 15
    cooldown_period   = 300
    min_replica_count = 0
    max_replica_count = 5
    behavior = {
      restore_to_original_replica_count = true
      stabilization_window_seconds      = 300
      type                              = "Percent"
      value                             = 100
      period_seconds                    = 15
    }
    triggers = [
      { type = "prometheus", threshold = 2 },
    ]
  }
}
```

The image tag has the form `python-<minor>-<pymonik-version>`. Find
available tags at the
[official Docker Hub repository](https://hub.docker.com/r/dockerhubaneo/harmonic_snake).

The Python minor in the tag **must match** your client's Python
minor — cloudpickle isn't cross-minor-compatible.

## Building your own image

Two reasons:

1. **Pinned dependencies** — your tasks import a fixed set of
   libraries. Baking them in skips the first-task install cost on
   every fresh pod.
2. **Multi-file project** — your tasks import from `mypkg.foo`. The
   worker needs `mypkg` importable; baking is the production answer.

Start from the official base image:

```dockerfile
ARG PYTHON_VERSION=3.11
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS base

RUN groupadd --gid 5000 armonikuser \
 && useradd --uid 5000 --gid 5000 --home-dir /home/armonikuser --create-home armonikuser \
 && mkdir /cache && chown armonikuser: /cache

USER armonikuser
WORKDIR /app

# Copy your project metadata + lockfile + source.
COPY --chown=armonikuser:armonikuser pyproject.toml uv.lock README.md* ./
COPY --chown=armonikuser:armonikuser src/ ./src/

# Build a frozen venv. `--no-dev` skips dev deps (pytest, ruff, ...).
RUN uv venv /app/.venv && uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# The worker entrypoint that ArmoniK launches.
ENTRYPOINT ["pymonik-worker"]
```

Build:

```sh
docker build -t my-org/my-pymonik-worker:v3 .
```

Push to whatever registry your cluster pulls from:

```sh
docker push my-org/my-pymonik-worker:v3
```

Update your Terraform `worker[0].image` and `tag`, apply, and
restart the partition:

```sh
kubectl rollout restart deployment/compute-plane-pymonik -n armonik
```

## What "must match" means concretely

For an image to function as a PymoniK worker:

- **Python minor** matches the client's. If your `pyproject.toml` has
  `requires-python = "==3.11.*"`, the image must run Python 3.11.
- **`pymonik` is installed** in the venv at `/app/.venv` (or wherever
  `PATH` points). The console script `pymonik-worker` must be on
  `PATH`.
- **`armonikuser` (uid 5000)** owns `/cache` and `/app` (the polling
  agent expects these paths writable by the same uid that
  `pymonik-worker` runs as).

The simplest sanity check: locally, `docker run --rm -it
<your-image> bash` and run `pymonik-worker --help`. If that prints
help, the image is structurally fine.

## Custom worker code

If you have a reason to add your own logic to the worker process —
metrics emission, custom signal handling, monkey-patching a library
before any task runs — write a small Python entrypoint that calls
`pymonik.worker.run()` and use that as the image's `ENTRYPOINT`:

```python
# my_worker.py
import logging

import pymonik
from pymonik.worker import run

def main() -> None:
    logging.getLogger("my_app").setLevel(logging.INFO)
    pymonik.enable_logging("INFO")
    # Your one-time setup here.
    run()

if __name__ == "__main__":
    main()
```

```dockerfile
# ... same base as above ...
COPY --chown=armonikuser:armonikuser my_worker.py /app/
ENTRYPOINT ["python", "/app/my_worker.py"]
```

`pymonik.worker.run()` does the same thing the `pymonik-worker`
console script does — registers the dispatch loop with the upstream
`armonik` worker framework and serves tasks until shut down.

## Future: `pymonik image build`

A `pymonik image build` CLI subcommand is planned:
read your `pyproject.toml`, render a Dockerfile from a template, run
`docker build`, print the tag. Until that lands, hand-write the
Dockerfile above; it's ~15 lines and changes rarely.
