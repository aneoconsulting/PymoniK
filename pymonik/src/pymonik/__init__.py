import importlib.metadata

from .core import Pymonik, Task, task
from .context import PymonikContext
from .results import ResultHandle, MultiResultHandle
from .worker import run_pymonik_worker
from armonik.common import TaskOptions

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0"  # Fallback for development mode

__all__ = [
    "Pymonik",
    "task",
    "PymonikContext",
    "run_pymonik_worker",
    "Task",
    "ResultHandle",
    "MultiResultHandle",
    "TaskOptions",
]
