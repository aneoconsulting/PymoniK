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


def materialize(source: Path | str, *, at: str) -> Materialize:
    """Upload ``source`` and request placement at ``at`` on the worker.

    Files: bytes are written to ``at`` before the task runs.
    Directories: contents are zipped (deflated) client-side, uploaded,
    and unpacked into ``at`` on the worker.

    ``at`` is absolute (or relative to the worker's working dir). The
    task parameter receives a ``pathlib.Path(at)``.
    """
    src = Path(source)
    if src.is_dir():
        data = _zip_directory(src)
        session = current_session()
        result_id = session._upload_blob(data)
        return Materialize(
            result_id, worker_path=at, size=len(data), is_dir=True
        )
    data = src.read_bytes()
    session = current_session()
    result_id = session._upload_blob(data)
    return Materialize(result_id, worker_path=at, size=len(data), is_dir=False)


def _zip_directory(root: Path) -> bytes:
    """Zip a directory into bytes, deterministically (sorted entries).

    Sorting keeps the SHA-256 stable, so re-uploads of the same dir
    contents dedup via the session's blob cache.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root))
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
