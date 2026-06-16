"""Blobs — first-class inputs that don't live inline in the task envelope.

Three flavours, one mental model: a ``Blob[T]`` is a typed handle whose
bytes live in ArmoniK's object store and flow into a task via
``data_dependencies``. The handle is what user code passes around on the
client; on the worker, the function receives the resolved value (not the
handle).

- ``blob.upload(obj)`` — cloudpickles a Python object, uploads, returns
  ``Blob[T]``. The task receives the object directly.

- ``blob.upload(Path("file"))`` — uploads raw file bytes, returns
  ``Blob[bytes]``. The task receives ``bytes``.

- ``blob.materialize(Path("local.toml"), at="/etc/app.toml")`` — uploads
  raw bytes and tells the worker to write them to ``/etc/app.toml``
  before the task runs. The task parameter receives a
  ``pathlib.Path("/etc/app.toml")`` pointing at the written file.

- ``blob.materialize(Path("./assets"), at="/opt/assets")`` — when
  ``source`` is a directory, the contents are zipped client-side,
  uploaded, and unpacked at ``at`` on the worker before the task runs.
  Task parameter receives ``pathlib.Path("/opt/assets")``.

Dedup: content is hashed (SHA-256). Two uploads of the same bytes in the
same session reuse the first result id — no re-upload. Cross-session
dedup is not implemented.

Size threshold: user code never needs to think about it. If a plain arg
exceeds ``PymonikClient(spill_threshold=...)`` (default 256 KiB) it's
auto-spilled to a Blob during submission. ``blob.upload(...)`` is the
explicit form — useful when the same blob will be reused across many
tasks (the dedup cache saves the upload round-trip).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any, Generic, TypeVar

import cloudpickle

from pymonik.task import current_session

T = TypeVar("T")


# Upload encoding — what the worker does with the downloaded bytes.
ENC_PICKLE = "pickle"  # cloudpickle.loads(bytes) → value
ENC_BYTES = "bytes"    # hand bytes straight to the task


class Blob(Generic[T]):
    """A typed handle to bytes stored in ArmoniK's object store."""

    __slots__ = ("_result_id", "_encoding", "_size")

    def __init__(self, result_id: str, *, encoding: str, size: int) -> None:
        self._result_id = result_id
        self._encoding = encoding
        self._size = size

    @property
    def result_id(self) -> str:
        return self._result_id

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def size(self) -> int:
        return self._size

    def __repr__(self) -> str:
        return f"<Blob {self._encoding} result_id={self._result_id!r} size={self._size}B>"


class Materialize:
    """A blob with a target on-worker path.

    When passed to a task, the worker materialises the bytes at
    ``worker_path``: for files (``is_dir=False``) it writes them
    directly; for directories (``is_dir=True``) it unpacks the zip
    contents into ``worker_path``. The task parameter receives a
    ``pathlib.Path`` to the materialised location.
    """

    __slots__ = ("_result_id", "_worker_path", "_size", "_is_dir")

    def __init__(
        self,
        result_id: str,
        *,
        worker_path: str,
        size: int,
        is_dir: bool = False,
    ) -> None:
        self._result_id = result_id
        self._worker_path = worker_path
        self._size = size
        self._is_dir = is_dir

    @property
    def result_id(self) -> str:
        return self._result_id

    @property
    def worker_path(self) -> str:
        return self._worker_path

    @property
    def size(self) -> int:
        return self._size

    @property
    def is_dir(self) -> bool:
        return self._is_dir

    def __repr__(self) -> str:
        kind = "dir" if self._is_dir else "file"
        return (
            f"<Materialize {kind} at={self._worker_path!r} "
            f"result_id={self._result_id!r} size={self._size}B>"
        )


# ---------- public API ----------

def upload(value: Any) -> Blob[Any]:
    """Upload a value to the current session's object store.

    If ``value`` is a ``bytes``/``bytearray`` or ``Path``, uploads raw bytes
    and the worker will receive ``bytes``. Otherwise cloudpickles the value
    and the worker will receive the deserialised object.
    """
    data, encoding = _encode(value)
    session = current_session()
    result_id = session._upload_blob(data)
    return Blob(result_id, encoding=encoding, size=len(data))


def materialize(
    source: Path | str, *, at: str, preserve_mtime: bool = False
) -> Materialize:
    """Upload ``source`` and request placement at ``at`` on the worker.

    Files: bytes are written to ``at`` before the task runs.
    Directories: contents are zipped (deflated) client-side, uploaded,
    and unpacked into ``at`` on the worker.

    ``at`` is absolute (or relative to the worker's working dir). The
    task parameter receives a ``pathlib.Path(at)``.

    ``preserve_mtime`` (directories only): by default file modification
    times are normalised in the archive so identical contents hash
    identically — the within-session blob cache then dedups re-uploads
    of the same tree. Pass ``preserve_mtime=True`` to fold each file's
    mtime into the archive instead, so re-materialising the same bytes
    with a newer timestamp produces a different hash and re-uploads
    (deliberate cache invalidation). No effect on single-file sources:
    a file is hashed by its raw bytes, which never carry the mtime.
    """
    src = Path(source)
    if src.is_dir():
        data = _zip_directory(src, preserve_mtime=preserve_mtime)
        session = current_session()
        result_id = session._upload_blob(data)
        return Materialize(
            result_id, worker_path=at, size=len(data), is_dir=True
        )
    data = src.read_bytes()
    session = current_session()
    result_id = session._upload_blob(data)
    return Materialize(result_id, worker_path=at, size=len(data), is_dir=False)


# Zip's epoch floor (DOS date). Pinning every entry here makes the archive
# bytes — and thus the SHA-256 — independent of file mtimes.
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _zip_directory(root: Path, *, preserve_mtime: bool = False) -> bytes:
    """Zip a directory into bytes with a stable entry order.

    Entries are sorted so ordering never perturbs the output. By default
    each entry's timestamp is pinned to a fixed epoch and only the file
    content and mode are carried, so the SHA-256 depends on *content*
    (and perms), not on mtimes — re-zipping an unchanged tree yields
    identical bytes, which the session blob cache dedups. Sorting alone
    does NOT achieve this: ``ZipFile.write`` stamps each entry with the
    file's real mtime, so the hash would otherwise shift whenever a file
    was touched.

    ``preserve_mtime=True`` keeps the real per-file mtimes in the
    archive, so a newer timestamp on otherwise-identical content changes
    the hash (deliberate cache invalidation).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            arcname = str(path.relative_to(root))
            if preserve_mtime:
                zf.write(path, arcname)
                continue
            info = zipfile.ZipInfo(arcname, date_time=_FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Carry the file's permission/type bits (matching ZipFile.write)
            # so executables stay executable on extraction. Perms are stable
            # across re-zips of the same tree, so the hash stays stable too.
            info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


# ---------- helpers ----------

def _encode(value: Any) -> tuple[bytes, str]:
    """Normalise a user-supplied value into (bytes, encoding)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), ENC_BYTES
    if isinstance(value, Path):
        return value.read_bytes(), ENC_BYTES
    return cloudpickle.dumps(value), ENC_PICKLE


def content_hash(data: bytes) -> str:
    """Stable hash used for within-session dedup and result naming."""
    return hashlib.sha256(data).hexdigest()
