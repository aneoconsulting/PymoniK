import io
import zipfile
import cloudpickle as pickle

from logging import Logger
from pathlib import Path
from typing import Any, Optional, Union


from .materialize import Materialize, _calculate_directory_hash, _calculate_file_hash
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

    @staticmethod
    def from_local(logger: Optional[Logger] = None) -> "PymonikContext":
        """
        Create a PymonikContext for local execution.
        """
        if logger is None:
            logger = Logger("PymonikLocalExecution")
        return PymonikContext(task_handler=None, logger=logger)

    def retrieve_object(
        self, 
        result_id: str, 
        auto_unpickle: bool = True, 
        check_exists: bool = True,
        force_retrieve: bool = False
    ) -> Union[bool, Any, bytes, None]:
        """
        Retrieves an object from ArmoniK storage to the local worker cache.
        
        Args:
            result_id (str): The ID of the result/object to retrieve
            auto_unpickle (bool): If True, automatically unpickle and return the object.
                                If False, just retrieve the file and return the bytes.
                                Defaults to True.
            check_exists (bool): If True, check if the object already exists locally 
                               before attempting to retrieve. Defaults to True.
            force_retrieve (bool): If True, retrieve the object even if it already exists
                                 locally. Only used when check_exists=True. Defaults to False.
        
        Returns:
            - If auto_unpickle=True: The unpickled object if successful, None if failed
            - If auto_unpickle=False: The raw bytes if successful, None if failed
            
        Raises:
            RuntimeError: If called in local context (no task handler available)
        """
        if self.is_local:
            raise RuntimeError("retrieve_object can only be called in worker context")
            
        object_path = self.get_object_path(result_id)
        
        # Check if object already exists locally
        if check_exists and object_path.exists():
            self.logger.info(f"=== DEBUG RETRIEVE: Object {result_id} already exists locally at {object_path} ===")
            
            if not force_retrieve:
                if auto_unpickle:
                    try:
                        with open(object_path, "rb") as fh:
                            return pickle.load(fh)
                    except Exception as e:
                        self.logger.error(f"Failed to unpickle existing object {result_id}: {e}")
                        return None
                else:
                    # Return the bytes from the existing file
                    try:
                        with open(object_path, "rb") as fh:
                            return fh.read()
                    except Exception as e:
                        self.logger.error(f"Failed to read existing object {result_id}: {e}")
                        return None
            else:
                self.logger.info(f"force_retrieve=True, retrieving {result_id} anyway")

        self.logger.info(f"=== DEBUG RETRIEVE: {result_id} not in data_dependencies, trying GetResourceData ===")
        try:
            # Ensure the parent directory exists
            object_path.parent.mkdir(parents=True, exist_ok=True)
            
            data_request = DataRequest(
                communication_token=self.task_handler.token, 
                result_id=result_id
            )
            
            # GetResourceData downloads the file directly to object_path
            data_response: DataResponse = self.task_handler._client.GetResourceData(data_request)
            
            if data_response.result_id != result_id:
                self.logger.error(f"Retrieved object ID mismatch: expected {result_id}, got {data_response.result_id}")
                return None
                
            # The file should now exist at object_path
            if not object_path.exists():
                self.logger.error(f"GetResourceData completed but file doesn't exist at {object_path}")
                return None
                
            self.logger.info(f"Successfully retrieved object {result_id} via GetResourceData to {object_path}")
            
            if auto_unpickle:
                try:
                    with open(object_path, "rb") as fh:
                        unpickled_obj = pickle.load(fh)
                        self.logger.debug(f"Successfully unpickled object {result_id}")
                        return unpickled_obj
                except Exception as e:
                    self.logger.error(f"Failed to unpickle object {result_id}: {e}")
                    return None
            else:
                # Return the raw bytes from the downloaded file
                try:
                    with open(object_path, "rb") as fh:
                        return fh.read()
                except Exception as e:
                    self.logger.error(f"Failed to read downloaded file {object_path}: {e}")
                    return None
                
        except Exception as e:
            self.logger.error(f"Failed to retrieve object {result_id} via GetResourceData: {e}")
            import traceback
            self.logger.error(f"=== DEBUG RETRIEVE: Traceback: {traceback.format_exc()} ===")
            return None

    def get_object_path(self, result_id: str) -> Path:
        """
        Get the local file path where an object would be stored.
        
        Args:
            result_id (str): The ID of the result/object
            
        Returns:
            Path: The local path where the object is/would be stored
        """
        return Path("/cache/shared/") / Path(self.task_handler.token) / Path(result_id)

    def object_exists_locally(self, result_id: str) -> bool:
        """
        Check if an object exists locally in the worker cache.
        
        Args:
            result_id (str): The ID of the result/object to check
            
        Returns:
            bool: True if the object exists locally, False otherwise
        """
        return self.get_object_path(result_id).exists()

    def materialize_file(self, mat: Materialize) -> bool:
        """
        Materialize a file/directory in the worker if needed.
        
        Args:
            mat: Materialize object describing what to materialize
            
        Returns:
            bool: True if materialization was successful, False otherwise
        """
        if self.is_local:
            self.logger.warning("materialize_file called in local context, skipping")
            return True
        
        worker_path = Path(mat.worker_path)
        
        # Check if file/directory already exists and has correct hash
        if worker_path.exists():
            try:
                if mat.is_directory and worker_path.is_dir():
                    existing_hash = _calculate_directory_hash(worker_path)
                elif not mat.is_directory and worker_path.is_file():
                    existing_hash = _calculate_file_hash(worker_path)
                else:
                    # Type mismatch (file vs directory), need to re-materialize
                    existing_hash = None
                
                if existing_hash == mat.content_hash:
                    self.logger.info(f"Materialize content already exists with correct hash: {worker_path}")
                    return True
                else:
                    self.logger.info(f"Materialize content exists but hash mismatch, re-materializing: {worker_path}")
            except Exception as e:
                self.logger.warning(f"Error checking existing materialize content: {e}")
        
        # Need to retrieve and materialize
        if not mat.result_id:
            self.logger.error(f"Materialize object has no result_id: {mat}")
            return False
        
        try:
            # Retrieve the content
            content_bytes = self.retrieve_object(mat.result_id, auto_unpickle=False, check_exists=False)
            if not content_bytes:
                self.logger.error(f"Failed to retrieve materialize content: {mat.result_id}")
                return False
            
            # Create parent directories
            worker_path.parent.mkdir(parents=True, exist_ok=True)
            
            if mat.is_directory:
                # Extract zip to target directory
                with zipfile.ZipFile(io.BytesIO(content_bytes), 'r') as zipf:
                    zipf.extractall(worker_path)
                self.logger.info(f"Extracted directory to: {worker_path}")
            else:
                # Write file directly
                with open(worker_path, 'wb') as f:
                    f.write(content_bytes)
                self.logger.info(f"Wrote file to: {worker_path}")
            
            # Verify hash after materialization
            if mat.is_directory:
                final_hash = _calculate_directory_hash(worker_path)
            else:
                final_hash = _calculate_file_hash(worker_path)
            
            if final_hash != mat.content_hash:
                self.logger.error(f"Hash mismatch after materialization: expected {mat.content_hash}, got {final_hash}")
                return False
            
            self.logger.info(f"Successfully materialized: {mat.source_path} -> {worker_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error materializing content: {e}")
            return False
