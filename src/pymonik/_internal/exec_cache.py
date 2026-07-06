"""Result-reuse cache.

Two pieces live here:

- **Structural cache keys.** A task call's key is content-addressed
  over the *graph identity*, computed at submit time:
  ``H(version, python, fn_identity, [arg descriptors])``. A ``Future``
  argument contributes the **upstream task's key** (carried on the
  future), not its not-yet-known value — so intermediate tasks are
  cacheable and an unchanged DAG prefix produces stable keys even when a
  downstream task changes. (Same content-addressing as Nix / Bazel /
  Nextflow ``-resume``.)
- **ResultIndex.** A client-side, disk-backed ``key → (result_id,
  session_id)`` map. A hit is validated against the cluster
  (result still ``COMPLETED``) before the existing ``result_id`` is
  reused as a dependency / lazily downloaded — no resubmission. There is
  no retention story: a stale entry just misses and we recompute.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import cloudpickle

from pymonik._internal._logging import get_logger

log = get_logger(__name__)


def python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def default_cache_dir() -> Path:
    """``~/.cache/pymonik`` on most platforms; honours XDG_CACHE_HOME."""
    xdg = os.getenv("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "pymonik"


# ---------- function identity ----------


def fn_identity(func: Any, *, cache_version: str | None = None) -> bytes | None:
    """A stable hash of *what the function computes*.

    - ``cache_version`` (from ``@task(cache_version=...)``) wins — the
      user declares identity explicitly; nothing else is inspected.
    - Otherwise the normalised **source** of the function plus the hashes
      of its closed-over free variables. Source-based (not cloudpickle
      bytes) so it's stable across runs and reasonably portable.
    - ``None`` (uncacheable) when neither source nor a deterministic
      closure hash is available.

    Caveat (the user's purity contract): this is a heuristic. It does not
    see changes inside helper functions the task *calls*; use
    ``cache_version`` to force a bust when that matters.
    """
    if cache_version is not None:
        return b"ver:" + cache_version.encode()
    try:
        src = textwrap.dedent(inspect.getsource(func)).strip()
    except (OSError, TypeError):
        # No source (builtin / C / some REPLs). Fall back to cloudpickle
        # bytes — less stable, but better than refusing to cache.
        try:
            return b"pk:" + hashlib.sha256(cloudpickle.dumps(func)).digest()
        except Exception:
            return None
    h = hashlib.sha256(b"src:" + src.encode())
    closure = getattr(func, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                h.update(hashlib.sha256(cloudpickle.dumps(cell.cell_contents)).digest())
            except Exception:
                return None  # a closed-over value we can't hash → uncacheable
    return h.digest()


# ---------- argument descriptors ----------


def arg_descriptor(value: Any) -> bytes | None:
    """A stable byte descriptor for one argument leaf.

    Returns ``None`` when the leaf makes the call uncacheable. A
    ``Future``/``MultiResultHandle`` contributes the *upstream task's
    cache key* (the Merkle link) rather than its value.
    """
    from pymonik.blob import Blob, Materialize
    from pymonik.future import Future, FutureList, MultiResultHandle

    if isinstance(value, Future):
        # Upstream must itself be cacheable for us to be — otherwise we
        # can't name the input deterministically.
        return b"F:" + value._cache_key.encode() if value._cache_key else None
    if isinstance(value, FutureList):
        parts: list[bytes] = [b"FL"]
        for f in value:
            if not f._cache_key:
                return None
            parts.append(f._cache_key.encode())
        return b":".join(parts)
    if isinstance(value, MultiResultHandle):
        # A handle as a whole isn't a single dependency; callers pass a
        # field future (handle.field), which is a Future. Reject the bare
        # handle as uncacheable.
        return None
    if isinstance(value, Blob):
        return f"B:{value.encoding}:{value.result_id}".encode()
    if isinstance(value, Materialize):
        return f"M:{value.result_id}:{value.worker_path}".encode()
    if isinstance(value, list):
        parts = [b"L"]
        for v in value:
            d = arg_descriptor(v)
            if d is None:
                return None
            parts.append(d)
        return b":".join(parts)
    if isinstance(value, tuple):
        parts = [b"T"]
        for v in value:
            d = arg_descriptor(v)
            if d is None:
                return None
            parts.append(d)
        return b":".join(parts)
    if isinstance(value, dict):
        parts = [b"D"]
        for k in sorted(value.keys(), key=lambda k: repr(k)):
            sub = arg_descriptor(value[k])
            if sub is None:
                return None
            parts.append(repr(k).encode())
            parts.append(sub)
        return b":".join(parts)
    # Plain leaf — cloudpickle hash (deterministic for plain data).
    try:
        return b"P" + hashlib.sha256(cloudpickle.dumps(value)).digest()
    except Exception:
        return None


def compute_cache_key(
    *,
    pymonik_version: str,
    task_name: str,
    fn_id: bytes | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str | None:
    """Structural key for a single task call, or ``None`` if uncacheable."""
    if fn_id is None:
        return None
    parts: list[bytes] = [
        b"v=" + pymonik_version.encode(),
        b"py=" + python_minor().encode(),
        b"task=" + task_name.encode(),
        b"fn=" + fn_id,
    ]
    for a in args:
        d = arg_descriptor(a)
        if d is None:
            return None
        parts.append(b"a=" + d)
    for k in sorted(kwargs.keys()):
        sub = arg_descriptor(kwargs[k])
        if sub is None:
            return None
        parts.append(f"k:{k}=".encode() + sub)
    return hashlib.sha256(b"||".join(parts)).hexdigest()


# ---------- result index (key -> result_id) ----------


class ResultIndex:
    """Disk-backed ``key → {result_id, session_id}`` map.

    One small JSON file per key under ``<root>/index/<ab>/<key>.json``.
    Atomic writes (tempfile + rename). A missing/garbage entry is a miss.
    """

    def __init__(self, root: Path) -> None:
        self._root = root / "index"

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, str] | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "result_id" not in data:
            return None
        return data

    def put(self, key: str, result_id: str, session_id: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"result_id": result_id, "session_id": session_id})
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, p)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def forget(self, key: str) -> None:
        with contextlib.suppress(OSError):
            self._path(key).unlink()


# ---------- value store (Layer 3: optional local value cache) ----------


class ExecCache:
    """Disk-backed *value* store for the optional local-value cache.

    Stores cloudpickled result bytes the user already downloaded via
    ``.result()``, keyed by the structural cache key. Atomic
    writes; unreadable entries are dropped and treated as misses.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        return self._root / "values" / key[:2] / f"{key}.pkl"

    def get_bytes(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise KeyError(key)
        try:
            return p.read_bytes()
        except OSError as e:
            raise KeyError(key) from e

    def put_bytes(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".pkl")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
