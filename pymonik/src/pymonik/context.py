from logging import Logger
from typing import Optional

from .environment import RuntimeEnvironment
from armonik.worker import TaskHandler

class PymonikContext:
    """
    Context for PymoniK execution.
    This class is used to manage the execution environment and logging for PymoniK tasks.
    When running in a local environment, it uses the provided logger.
    """
    def __init__(self, task_handler: TaskHandler, logger: Logger):
        self.task_handler = task_handler
        self.logger = logger
        self.environment = RuntimeEnvironment(logger)
        self.is_local = task_handler is None

    def from_local(logger: Optional[Logger] = None) -> "PymonikContext":
        """
        Create a PymonikContext for local execution.
        """
        if logger is None:
            logger = Logger("PymonikLocalExecution")
        return PymonikContext(task_handler=None, logger=logger)
