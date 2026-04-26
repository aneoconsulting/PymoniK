# Blobs and Materialize

A `Blob[T]` is a typed handle to bytes that live in ArmoniK's object
store. The handle is what your client passes around; on the worker,
the function receives the resolved value (or a path, for
`Materialize`).

Three shapes, one mental model:

- `blob.upload(obj)` — cloudpickles a Python object and uploads.
  Worker function receives the object.
- `blob.upload(Path("file"))` — uploads raw file bytes. Worker
  function receives `bytes`.
- `blob.materialize(...)` — uploads bytes (or a zipped directory) and
  asks the worker to write them to a path on disk. Worker function
  receives a `pathlib.Path`.

## Why use blobs explicitly

Most arguments don't need this — PymoniK auto-spills any cloudpickled
arg above the threshold (default 256 KiB) to a blob during
submission, transparently. You only reach for `blob.upload` when you
want to **share the same bytes across many tasks** and skip
re-uploading:

```python
import pymonik.blob as blob

with client.session(partition="pymonik") as s:
    weights = blob.upload(Path("model.bin"))   # uploaded once
    for shard in shards:
        infer.spawn(weights, shard)            # all tasks share the blob_id
```

The session's blob cache deduplicates by SHA-256 of the bytes — even
without the explicit `blob.upload`, two `.spawn()` calls passing the
same large value would dedupe at auto-spill time.

## Files: bytes on the worker

```python
import pymonik.blob as blob
from pymonik import Blob, task
from pathlib import Path

@task
def parse_config(data: bytes) -> dict:
    import tomllib
    return tomllib.loads(data.decode())

with client.session(partition="pymonik") as s:
    cfg = blob.upload(Path("config.toml"))     # Blob[bytes]
    parse_config.spawn(cfg).result()
```

`blob.upload(Path)` reads file bytes verbatim. The function receives
`bytes`; what you do with them is your call.

## Materialize: write a file at a path on the worker

When a library you call needs a file path on disk, `materialize`
puts the bytes there:

```python
from pymonik import task
import pymonik.blob as blob
from pathlib import Path

@task
def run_with_config(config_path: Path) -> str:
    return Path(config_path).read_text()

with client.session(partition="pymonik") as s:
    cfg = blob.materialize(Path("./local.toml"), at="/etc/app.toml")
    run_with_config.spawn(cfg).result()
```

The worker writes the bytes to `/etc/app.toml` *before* the task
runs, then passes `pathlib.Path("/etc/app.toml")` as the argument.
Parent directories are created if they don't exist.

## Materialize a whole directory

`blob.materialize(dir_path, at=...)` detects a directory and zips it
client-side:

```python
import pymonik.blob as blob

@task
def use_assets(assets_dir: Path) -> list[str]:
    return [p.name for p in assets_dir.rglob("*") if p.is_file()]

with client.session(partition="pymonik") as s:
    assets = blob.materialize(Path("./assets"), at="/opt/assets")
    files = use_assets.spawn(assets).result()
```

The zip happens client-side with deterministic ordering (sorted
`rglob`) so two uploads of the same directory contents produce
identical bytes — and identical `result_id`s, so the session's blob
cache deduplicates them. On the worker, the bytes are unpacked into
`/opt/assets` and the task receives a `Path` to it.

Limits to be aware of:

- The zip is held in memory client-side. Multi-GB assets will hurt;
  consider baking those into the worker image instead.
- File permissions inside the zip are normalised by Python's
  `zipfile`. If you need executable bits or symlinks, materialise
  individual files yourself and set `chmod` inside the task.
- Existing files at the target path are overwritten by `extractall`.

## Auto-spill: when arguments get too big

You don't have to call `blob.upload` for arguments to flow through
the object store. PymoniK cloudpickles each top-level positional /
keyword argument and uploads anything above `spill_threshold`
(default 256 KiB) automatically:

```python
PymonikClient(spill_threshold=64 * 1024)   # spill arg > 64 KiB
```

The function receives the deserialised object exactly as if it had
been passed inline. Sub-elements aren't introspected — a list with
a million ints spills as one blob, not a million.

Auto-spill has the same dedup behaviour: two tasks receiving the
same big value share one blob upload.

## Cross-session blob reuse (planned)

Within a session, identical bytes upload once. Across sessions, each
session re-uploads. A planned mechanism using ArmoniK's
`Results.import_data` will let a fresh result id bind to data already
in the object store from a prior session. Until that lands: re-upload,
or bake large static assets into the worker image.
