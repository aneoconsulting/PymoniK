"""PymoniK — an easy-to-start SDK for ArmoniK.

Quick start:

    from pymonik import PymonikClient, task

    @task
    def add(a: int, b: int) -> int:
        return a + b

    @task
    def sum_all(xs: list[int]) -> int:
        return sum(xs)

    with PymonikClient() as client:                        # reads AKCONFIG
        with client.session(partition="pymonik") as s:
            # Pipelining: pass futures as args. No client-side blocking — ArmoniK
            # chains the tasks via data_dependencies. Only the terminal .result()
            # actually waits.
            parts = add.map(range(16), range(1, 17))
            total = sum_all.spawn(parts)
            print(total.result(timeout=60))
"""

from pymonik import blob, hooks, testing
from pymonik._internal._logging import enable_logging, silence_logging
from pymonik._internal.info import (
    PartitionInfo,
    ResultInfo,
    SessionInfo,
    TaskInfo,
)
from pymonik._internal.query import (
    PartitionQuery,
    ResultQuery,
    SessionQuery,
    TaskQuery,
)
from pymonik.blob import Blob, Materialize
from pymonik.client import PymonikClient
from pymonik.composition import (
    as_completed,
    gather,
)
from pymonik.context import Ctx, WorkerContext, current
from pymonik.errors import (
    ConnectionError as PymonikConnectionError,
)
from pymonik.errors import (
    NotInSessionError,
    PymonikError,
    TaskCancelled,
    TaskFailed,
    TaskTimeout,
)
from pymonik.future import (
    Future,
    FutureList,
    MultiResultHandle,
    MultiResultView,
    Outcome,
)
from pymonik.multiresult import MultiResult, TailPromise
from pymonik.options import TaskOpts
from pymonik.task import Task, task

__all__ = [
    "PymonikClient",
    "task",
    "Task",
    "TaskOpts",
    "Future",
    "FutureList",
    "MultiResult",
    "MultiResultHandle",
    "MultiResultView",
    "Outcome",
    "TailPromise",
    "gather",
    "as_completed",
    "current",
    "WorkerContext",
    "Ctx",
    "blob",
    "Blob",
    "Materialize",
    "testing",
    "hooks",
    "enable_logging",
    "silence_logging",
    # introspection
    "TaskQuery",
    "ResultQuery",
    "SessionQuery",
    "PartitionQuery",
    "TaskInfo",
    "ResultInfo",
    "SessionInfo",
    "PartitionInfo",
    # errors
    "PymonikError",
    "TaskFailed",
    "TaskCancelled",
    "TaskTimeout",
    "NotInSessionError",
    "PymonikConnectionError",
    "__version__",
]

# Single source of truth: the installed package metadata, which
# uv-dynamic-versioning computes from git tags at build time. No
# hand-maintained string here (that's what used to drift from pyproject).
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("pymonik")
    del _pkg_version
except Exception:  # not installed (e.g. imported from a raw checkout)
    __version__ = "0.0.0+unknown"
