"""Typed exception hierarchy. Every error raised by PymoniK inherits from PymonikError."""

from __future__ import annotations


class PymonikError(Exception):
    """Root of the PymoniK exception hierarchy."""


class ConnectionError(PymonikError):
    """Failed to reach the ArmoniK control plane."""


class NotInSessionError(PymonikError):
    """A task was spawned outside an open session context."""


class TaskFailed(PymonikError):
    """The worker raised an exception while executing the task.

    Holds the task_id and the worker-side error message (the traceback, if any).
    """

    def __init__(self, task_id: str, message: str) -> None:
        super().__init__(f"task {task_id} failed: {message}")
        self.task_id = task_id
        self.worker_message = message


class TaskCancelled(PymonikError):
    """Task was cancelled (session cancel, explicit cancel, or result aborted)."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id} cancelled")
        self.task_id = task_id


class TaskTimeout(PymonikError):
    """Task exceeded its max_duration."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id} timed out")
        self.task_id = task_id
