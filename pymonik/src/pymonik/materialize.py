        # if pymonik is None:
        #     pymonik = _CURRENT_PYMONIK.get(None)
        #     if pymonik is None:
        #         raise RuntimeError(
        #             "No active PymoniK instance found. Please create one and pass it in or use the context manager."
        #         )



import hashlib
import io
import os
import zipfile
from pathlib import Path
from typing import Optional, Union
import cloudpickle as pickle
from dataclasses import dataclass


@dataclass
class Materialize:
    """
    Represents a file or directory that should be materialized in the worker.
    Files are stored content-addressably using SHA-256 hashes.
    """
    source_path: str  # Original local path
    worker_path: str  # Target path in worker
    content_hash: str  # SHA-256 hash of the content
    is_directory: bool  # Whether the source was a directory (and thus zipped)
    result_id: Optional[str] = None  # Set after upload to ArmoniK
    
    def __post_init__(self):
        # Ensure paths are normalized
        self.source_path = str(Path(self.source_path).resolve())
        self.worker_path = str(Path(self.worker_path))


def _calculate_file_hash(file_path: Union[str, Path]) -> str:
    """Calculate SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _calculate_directory_hash(dir_path: Union[str, Path]) -> str:
    """Calculate SHA-256 hash of a directory by hashing its zipped contents."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        dir_path = Path(dir_path)
        for file_path in sorted(dir_path.rglob('*')):
            if file_path.is_file():
                arcname = file_path.relative_to(dir_path)
                zipf.write(file_path, arcname)
    
    zip_buffer.seek(0)
    hasher = hashlib.sha256()
    for chunk in iter(lambda: zip_buffer.read(8192), b""):
        hasher.update(chunk)
    return hasher.hexdigest()


def _create_zip_from_directory(dir_path: Union[str, Path]) -> bytes:
    """Create a zip file from a directory and return its bytes."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        dir_path = Path(dir_path)
        for file_path in sorted(dir_path.rglob('*')):
            if file_path.is_file():
                arcname = file_path.relative_to(dir_path)
                zipf.write(file_path, arcname)
    
    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def materialize(source_path: Union[str, Path], worker_path: Union[str, Path]) -> Materialize:
    """
    Create a Materialize object for a file or directory.
    
    Args:
        source_path: Local file or directory path to materialize
        worker_path: Target path in the worker where the file/directory should be placed
        
    Returns:
        Materialize: Object representing the materialized content
        
    Raises:
        FileNotFoundError: If source_path doesn't exist
        ValueError: If source_path is neither a file nor directory
    """
    source_path = Path(source_path)
    
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source_path}")
    
    if source_path.is_file():
        content_hash = _calculate_file_hash(source_path)
        is_directory = False
    elif source_path.is_dir():
        content_hash = _calculate_directory_hash(source_path)
        is_directory = True
    else:
        raise ValueError(f"Source path must be a file or directory: {source_path}")
    
    return Materialize(
        source_path=str(source_path),
        worker_path=str(worker_path),
        content_hash=content_hash,
        is_directory=is_directory
    )
