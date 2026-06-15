"""Arg references — sentinels that replace structured inputs on the wire.

Three kinds:

- ``FutureRef`` — another task's eventual result. Wired as a
  data_dependency; worker receives the upstream value after ArmoniK
  downloads the result bytes.

- ``BlobRef`` — a Blob uploaded via ``pymonik.blob.upload(...)`` or
  produced by auto-spill. Same ArmoniK mechanism as FutureRef; the
  encoding tells the worker whether to unpickle (for Python objects) or
  hand the raw bytes to the function.

- ``MaterializeRef`` — like a BlobRef but the worker writes the bytes to
  a file on disk at ``worker_path`` and the function receives a
  ``pathlib.Path`` to that file.

Walker is recursive through ``list``, ``tuple``, ``dict``, ``FutureList``.
Other container types pass through unchanged (add if a user actually
needs them).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import cloudpickle

from pymonik.blob import ENC_BYTES, ENC_PICKLE, Blob, Materialize

if TYPE_CHECKING:
    pass


class FutureRef:
    """Sentinel standing in for a Future during pickle/unpickle."""

    __slots__ = ("result_id",)

    def __init__(self, result_id: str) -> None:
        self.result_id = result_id

    def __repr__(self) -> str:
        return f"FutureRef({self.result_id!r})"


class BlobRef:
    """Sentinel standing in for a Blob. ``encoding`` tells the worker what
    type to surface to the function: ``"pickle"`` → the unpickled object,
    ``"bytes"`` → raw bytes.
    """

    __slots__ = ("result_id", "encoding")

    def __init__(self, result_id: str, encoding: str) -> None:
        self.result_id = result_id
        self.encoding = encoding

    def __repr__(self) -> str:
        return f"BlobRef({self.result_id!r}, encoding={self.encoding!r})"


class MaterializeRef:
    """Sentinel standing in for a Materialize — bytes materialised at
    ``worker_path`` before the task runs (file write, or zip-unpack when
    ``is_dir=True``); ``pathlib.Path(worker_path)`` substituted in as the
    argument value.
    """

    __slots__ = ("result_id", "worker_path", "is_dir")

    def __init__(self, result_id: str, worker_path: str, *, is_dir: bool = False) -> None:
        self.result_id = result_id
        self.worker_path = worker_path
        self.is_dir = is_dir

    def __repr__(self) -> str:
        kind = "dir" if self.is_dir else "file"
        return f"MaterializeRef({self.result_id!r}, at={self.worker_path!r}, {kind})"


# ---------- client-side: turn user values into refs + deps ----------

def extract_deps(value: Any, deps: list[str]) -> Any:
    """Recursive walk, replacing Future/Blob/Materialize with their ref
    sentinels and appending the referenced result_ids to ``deps``.

    Does NOT handle auto-spill — the session runs that as a second pass
    so it has the pickled bytes on hand to upload directly.
    """
    # Local imports avoid circulars at module load.
    from pymonik.future import Future, FutureList

    if isinstance(value, Future):
        deps.append(value.result_id)
        return FutureRef(value.result_id)
    if isinstance(value, Blob):
        deps.append(value.result_id)
        return BlobRef(value.result_id, encoding=value.encoding)
    if isinstance(value, Materialize):
        deps.append(value.result_id)
        return MaterializeRef(
            value.result_id, worker_path=value.worker_path, is_dir=value.is_dir
        )
    if isinstance(value, FutureList):
        return [extract_deps(v, deps) for v in value]
    if isinstance(value, list):
        return [extract_deps(v, deps) for v in value]
    if isinstance(value, tuple):
        return tuple(extract_deps(v, deps) for v in value)
    if isinstance(value, dict):
        return {k: extract_deps(v, deps) for k, v in value.items()}
    return value


def is_ref(value: Any) -> bool:
    return isinstance(value, (FutureRef, BlobRef, MaterializeRef))


# ---------- auto-spill: pickle top-level args, upload oversize ones ----------

def auto_spill(
    value: Any,
    deps: list[str],
    *,
    upload_blob: Callable[[bytes], str],
    threshold: int,
) -> Any:
    """Top-level spill for one positional arg or kwarg value.

    If ``value`` is already a ref sentinel, pass through. Otherwise
    cloudpickle it; if the blob exceeds ``threshold``, upload it and
    replace with a ``BlobRef`` sentinel. Sub-container elements are NOT
    examined individually — a large list is uploaded as a whole rather
    than split up.
    """
    if is_ref(value):
        return value
    buf = cloudpickle.dumps(value)
    if len(buf) <= threshold:
        return value
    result_id = upload_blob(buf)
    deps.append(result_id)
    return BlobRef(result_id, encoding=ENC_PICKLE)


# ---------- worker-side: resolve refs back to concrete values ----------

def resolve_refs(value: Any, data_dependencies: dict[str, bytes]) -> Any:
    """Walk ``value`` recursively, replacing each ref with its concrete value."""
    if isinstance(value, FutureRef):
        return cloudpickle.loads(data_dependencies[value.result_id])

    if isinstance(value, BlobRef):
        raw = data_dependencies[value.result_id]
        if value.encoding == ENC_PICKLE:
            # Auto-spill pickles a whole top-level container — including any
            # nested Future/Blob/Materialize sentinels extract_deps already
            # rewrote — into this single blob. Re-walk the unpickled value so
            # those inner refs resolve too; their bytes are present because
            # extract_deps appended their ids to the task's data_dependencies
            # before the spill pass ran. Without this recursion, a nested ref
            # inside a spilled container reaches the task as a raw sentinel
            # (silent wrong result). A concrete (non-container) spill — the
            # common case, e.g. a big array — short-circuits on the fallthrough
            # below, so this costs nothing there.
            return resolve_refs(cloudpickle.loads(raw), data_dependencies)
        if value.encoding == ENC_BYTES:
            return raw
        raise ValueError(f"unknown blob encoding: {value.encoding!r}")

    if isinstance(value, MaterializeRef):
        raw = data_dependencies[value.result_id]
        if value.is_dir:
            _unzip_materialized(value.worker_path, raw)
        else:
            _write_materialized(value.worker_path, raw)
        return Path(value.worker_path)

    if isinstance(value, list):
        return [resolve_refs(v, data_dependencies) for v in value]
    if isinstance(value, tuple):
        return tuple(resolve_refs(v, data_dependencies) for v in value)
    if isinstance(value, dict):
        return {k: resolve_refs(v, data_dependencies) for k, v in value.items()}

    return value


def _write_materialized(worker_path: str, data: bytes) -> None:
    # Create parent dirs if the caller wrote e.g. at="/tmp/cfg/app.toml".
    parent = os.path.dirname(worker_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(worker_path, "wb") as f:
        f.write(data)


def _unzip_materialized(worker_path: str, data: bytes) -> None:
    """Unpack zipped directory bytes into ``worker_path``.

    Creates ``worker_path`` if missing. Files inside the archive land at
    ``<worker_path>/<archive_relpath>``. Existing files at the same path
    are overwritten.
    """
    import io
    import zipfile

    os.makedirs(worker_path, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(worker_path)
