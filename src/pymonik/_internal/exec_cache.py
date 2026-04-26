"""On-disk execution cache.

Opt-in via ``PymonikClient(cache=...)`` (enables the cache *infrastructure*)
plus ``@task(cache=True)`` (opts a specific task in). When both are set,
``Task.spawn(...)`` / ``Task.map(...)`` compute a content hash of the
``(function, args, kwargs)`` triple and consult the cache *before*
submitting. A hit returns a ``Future`` that's already resolved with the
cached value — zero RPCs, zero workers scheduled. A miss submits as
normal and writes the result back when it lands.

Layout
------

::

    <root>/
        ab/
            ab12cd34…ef.pkl     # cloudpickled task return value
        cd/
            ...

Two-char prefix avoids one giant directory; key is the SHA-256 hex of
the canonicalised hash inputs.

What's safe to cache
--------------------

The user is responsible for declaring a task pure (``@task(cache=True)``).
Caching skips automatically when an arg can't be hashed deterministically:

- ``Future`` and ``FutureList`` args → upstream value not yet known; we
  can't compute a stable key without waiting.
- Anything cloudpickle can't dump.

Blob and Materialize args contribute their content hash (stable across
sessions and machines), so they participate in cache keys without
forcing the whole call to be a miss.

The key prefix includes ``pymonik.__version__`` and ``python_minor`` so
upgrading either invalidates entries cleanly.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cloudpickle
from pymonik._internal._logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


def python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def default_cache_dir() -> Path:
    """``~/.cache/pymonik`` on most platforms; honours XDG_CACHE_HOME."""
    xdg = os.getenv("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "pymonik"


def _hash_arg(value: Any) -> bytes | None:
    """Hash one arg-tree leaf. Returns ``None`` when the leaf makes the
    call uncacheable (typically: contains a ``Future``).
    """
    # Local imports keep this module light at top-level.
    from pymonik.blob import Blob, Materialize
    from pymonik.future import Future, FutureList

    if isinstance(value, (Future, FutureList)):
        return None  # upstream value not known yet — can't hash
    if isinstance(value, Blob):
        # blob.result_id is content-addressed locally (``local-blob-<sha>``)
        # or stable per-session on the cluster — both are safe inputs.
        return f"B:{value.encoding}:{value.result_id}".encode()
    if isinstance(value, Materialize):
        return f"M:{value.result_id}:{value.worker_path}".encode()
    if isinstance(value, list):
        parts: list[bytes] = [b"L"]
        for v in value:
            h = _hash_arg(v)
            if h is None:
                return None
            parts.append(h)
        return b":".join(parts)
    if isinstance(value, tuple):
        parts = [b"T"]
        for v in value:
            h = _hash_arg(v)
            if h is None:
                return None
            parts.append(h)
        return b":".join(parts)
    if isinstance(value, dict):
        parts = [b"D"]
        for k in sorted(value.keys(), key=lambda k: repr(k)):
            sub = _hash_arg(value[k])
            if sub is None:
                return None
            parts.append(repr(k).encode())
            parts.append(sub)
        return b":".join(parts)
    # Leaf — cloudpickle hash. Cloudpickle is deterministic for plain
    # data; for closures the bytes capture the identity.
    try:
        return b"P" + hashlib.sha256(cloudpickle.dumps(value)).digest()
    except Exception:
        return None


def compute_cache_key(
    *,
    pymonik_version: str,
    task_name: str,
    function_pickle_hash: bytes,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str | None:
    """Stable hash for ``(version, python, task, function, args, kwargs)``.

    Returns ``None`` when any arg makes the call uncacheable.
    """
    parts: list[bytes] = [
        b"v=" + pymonik_version.encode(),
        b"py=" + python_minor().encode(),
        b"task=" + task_name.encode(),
        b"fn=" + function_pickle_hash,
    ]
    for a in args:
        h = _hash_arg(a)
        if h is None:
            return None
        parts.append(b"a=" + h)
    for k in sorted(kwargs.keys()):
        sub = _hash_arg(kwargs[k])
        if sub is None:
            return None
        parts.append(f"k:{k}=".encode() + sub)
    return hashlib.sha256(b"||".join(parts)).hexdigest()


class ExecCache:
    """Disk-backed result cache.

    Atomic writes via tempfile + rename — a crashed write leaves no
    half-file in the cache. Reads that fail to unpickle (post-upgrade
    incompatibility, partial old entry, etc.) are treated as misses
    and the bad file is removed.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.pkl"

    def get_bytes(self, key: str) -> bytes:
        """Return the cloudpickled bytes for ``key`` or raise ``KeyError``."""
        p = self._path(key)
        if not p.exists():
            raise KeyError(key)
        try:
            return p.read_bytes()
        except OSError as e:
            raise KeyError(key) from e

    def get(self, key: str) -> Any:
        """Decoded equivalent of :meth:`get_bytes`."""
        raw = self.get_bytes(key)
        try:
            return cloudpickle.loads(raw)
        except Exception as e:
            # Post-upgrade-style incompatibility — drop and miss.
            try:
                self._path(key).unlink()
            except OSError:
                pass
            log.warning(
                "cache entry unreadable; dropped",
                key=key[:16],
                error=str(e),
            )
            raise KeyError(key) from e

    def put_bytes(self, key: str, data: bytes) -> None:
        """Atomic write of ``data`` (already cloudpickled) at ``key``."""
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmpname = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".pkl")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmpname, p)
        except Exception:
            try:
                os.unlink(tmpname)
            except OSError:
                pass
            raise

    def clear(self) -> int:
        """Delete every entry. Returns the number of files removed."""
        count = 0
        if not self._root.exists():
            return 0
        for sub in self._root.iterdir():
            if sub.is_dir():
                for f in sub.iterdir():
                    if f.suffix == ".pkl":
                        try:
                            f.unlink()
                            count += 1
                        except OSError:
                            pass
                try:
                    sub.rmdir()
                except OSError:
                    pass
        return count

    def stats(self) -> dict[str, int]:
        """Return ``{"entries": N, "bytes": M}`` for the current cache."""
        entries = 0
        total = 0
        if not self._root.exists():
            return {"entries": 0, "bytes": 0}
        for sub in self._root.iterdir():
            if sub.is_dir():
                for f in sub.iterdir():
                    if f.suffix == ".pkl":
                        try:
                            total += f.stat().st_size
                            entries += 1
                        except OSError:
                            pass
        return {"entries": entries, "bytes": total}
