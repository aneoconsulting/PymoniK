# Runtime environment

PymoniK lets you control the Python environment a task runs in
without rebuilding the worker image: install pip dependencies on
demand, set environment variables, point at a private package index.
Useful when you want to iterate on code that depends on libraries
that aren't in the base worker image.

## Adding pip dependencies

Declare them on the session:

```python
with client.session(
    partition="pymonik",
    deps=["numpy", "polars>=1", "scikit-learn==1.5.*"],
) as s:
    ...
```

`deps` is a list of [PEP 508](https://peps.python.org/pep-0508/)
specifier strings — exactly what you'd put in `requirements.txt`.

The first task into a worker pod with these deps installs them into
a content-addressed venv at `/cache/internal/envs/<env_id>/.venv`
(via `uv pip install`). Subsequent tasks reuse that venv with no
install cost. Two sessions on the same cluster declaring the same
`deps` resolve to the same `env_id` and share the same venv.

The wire footprint is the deps strings only — never a lockfile.
ArmoniK's polling agent caches the venv across tasks within a pod's
lifetime; pod restarts wipe it.

## Per-task overrides

Different tasks in one session can declare extra deps:

```python
@task(deps=["torch==2.6"])
def gpu_inference(x): ...

@task                          # no deps — uses session's set
def cheap_aggregate(xs): ...
```

Or per call via `with_options`:

```python
heavy = analyze.with_options(deps=["polars>=1.20"])
heavy.spawn(df).result()
```

Merge order is the same as for other options: session ← `@task` ←
`.with_options`.

## How tasks run with deps: subprocess vs in-process

When `deps` is non-empty, the worker has two modes for actually
running the task:

| Mode | When to use | How it works |
|------|-------------|--------------|
| **In-process splice** (default, `isolate=False`) | Compute-light tasks, single session per pod. ~1 ms per task once warm. | Worker adds the env's `site-packages` to `sys.path` and calls the function inline. |
| **Subprocess** (`isolate=True`) | Concurrent sessions on the same pod with conflicting deps, or tasks that mutate global module state. | Worker spawns a fresh Python interpreter from the env's venv per task. ~400-500 ms startup with numpy. |

Default is in-process splice because it's drastically faster for the
common case (numpy alone costs ~400 ms to import; subprocess pays
that on every task). The trade-off: import state persists across
tasks on the same pod. Two tasks in the same session that import
`numpy` see the same module; if a third task imported a *different*
numpy version on the same pod, the first import would win.

Opt into subprocess isolation when that matters:

```python
with client.session(
    partition="pymonik",
    deps=["torch==2.6"],
    isolate=True,
) as s:
    ...
```

## Environment variables

Set per-session env vars alongside (or instead of) deps:

```python
with client.session(
    partition="pymonik",
    deps=["numpy"],
    env={"OMP_NUM_THREADS": "4", "MY_FEATURE_FLAG": "true"},
) as s:
    ...
```

Per-task and per-call overrides work the same way (`@task(env=...)`,
`.with_options(env=...)`). Merges are key-wise — the per-task dict
adds to / overrides the session's, it doesn't replace it.

`env` works *with or without* `deps`. If you don't need extra packages
but want env vars on a task, `client.session(env={...})` alone is
enough — no venv is built.

Env vars participate in the `env_id` hash when deps are also
declared, so two sessions with the same deps but different env vars
get distinct venvs. This is intentional: env vars often change install
behaviour (CUDA build selection, `PIP_INDEX_URL`, etc.), and treating
them as part of identity prevents accidental cross-contamination.

## Private package indexes

```python
with client.session(
    partition="pymonik",
    deps=["my-private-pkg>=2"],
    index_url="https://idx.example.com/simple/",
) as s:
    ...
```

`index_url` is forwarded to `uv pip install --index-url`. For
indexes that need credentials, either bake the credential into the
URL (`https://user:token@idx.example.com/`) — keeping in mind that
`index_url` is part of the `env_id` hash, so two URLs that differ
only in credential will produce different venvs — or set
`UV_INDEX_URL` / `UV_EXTRA_INDEX_URL` in the worker pod environment
where it stays out of envelope payloads.

## When to use this vs baking an image

| Use `deps=` (this guide) | Bake an image |
|--------------------------|---------------|
| Iterating on what packages your tasks need | Production deploys you want pinned |
| Mixing different deps in different sessions on shared workers | The same set every time |
| Tasks that import third-party libraries | Tasks that import third-party libraries *plus your own multi-file project* |
| Need a private package on a one-off basis | C-extension libraries with system deps you'd otherwise need to install at runtime |

For multi-file projects, see the
[multi-file projects section in Important considerations](../important-considerations.md#cloudpickle-and-multi-file-projects).
For image baking, see [Worker images](worker-images.md).

## Inspecting the cache

Venvs live at `/cache/internal/envs/<env_id>/` on the worker pod.
The `env_id` is logged at submission time when the events stream
delivers a result, and it's stable across runs given the same deps —
so you can grep your worker logs for it.

Eviction is the polling agent's responsibility. A pod restart wipes
the cache (it's an `emptyDir`); a fresh pod re-runs `uv pip install`
on the first task with that env_id. Wheel downloads are shared across
env builds via `UV_CACHE_DIR=/cache/internal/uv`, so the second
session to install torch on a given pod pays the install but not the
download.
