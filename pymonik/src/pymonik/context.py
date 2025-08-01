from logging import Logger
from pathlib import Path
from typing import Optional

from .environment import RuntimeEnvironment
from armonik.worker import TaskHandler
from armonik.protogen.common.agent_common_pb2 import (DataRequest, DataResponse)


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

    def retrieve_object(self, result_id: str) -> bool:
        """Retrieves an object onto a folder with the result id as its name, returns true if successfully retrieved"""
        data_request = DataRequest(communication_token=self.task_handler.token, result_id=result_id)
        data_response: DataResponse = self.task_handler._client.GetResourceData(data_request)
        
        return data_response.result_id == result_id

    def get_object_path(self, result_id: str) -> Path:
        return Path("/cache/shared/")/ Path(self.task_handler.token) / Path(result_id)
