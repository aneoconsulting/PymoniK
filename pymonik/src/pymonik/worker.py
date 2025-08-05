import cloudpickle as pickle

from .materialize import Materialize
from .core import Pymonik
from .context import PymonikContext
from .environment import RuntimeEnvironment
from .results import ResultHandle, MultiResultHandle

from armonik.common import Output
from armonik.worker import TaskHandler, armonik_worker, ClefLogger
def _process_materialize_args(func_name, retrieved_args, task_handler, logger):
    """
    Process task arguments to find and materialize any Materialize objects.
    This should be called in run_pymonik_worker before executing the task.
    """
    try:
        logger.debug(f"Starting _process_materialize_args for {func_name}")
        logger.debug(f"Retrieved args count: {len(retrieved_args)}")
        
        # Log each argument type
        for i, arg in enumerate(retrieved_args):
            logger.debug(f"Arg {i}: type={type(arg)}, value={repr(arg) if not isinstance(arg, Materialize) else f'Materialize(source={arg.source_path}, worker={arg.worker_path}, hash={arg.content_hash}, result_id={arg.result_id})'}")
        
        # Create context for materialization
        ctx = PymonikContext(task_handler, logger)
        logger.debug(f"Created PymonikContext, is_local={ctx.is_local}")
        
        materialize_count = 0
        for i, arg in enumerate(retrieved_args):
            if isinstance(arg, Materialize):
                logger.debug(f"Found Materialize argument at position {i}: {arg.source_path} -> {arg.worker_path}")
                logger.debug(f"Materialize result_id: {arg.result_id}")
                logger.debug(f"Materialize content_hash: {arg.content_hash}")
                logger.debug(f"Materialize is_directory: {arg.is_directory}")
                
                success = ctx.materialize_file(arg)
                if success:
                    materialize_count += 1
                    logger.debug(f"Successfully materialized: {arg.worker_path}")
                else:
                    logger.error(f"Failed to materialize: {arg.worker_path}")
                    # Note: We don't fail the task, just log the error
                    # The task will receive the Materialize object and can handle the failure
            else:
                logger.debug(f"Arg {i} is not a Materialize object (type: {type(arg)})")
        
        if materialize_count > 0:
            logger.debug(f"Processed {materialize_count} Materialize objects for task {func_name}")
        else:
            logger.debug(f"No Materialize objects found in {len(retrieved_args)} arguments for task {func_name}")
            
    except Exception as e:
        logger.error(f"Error processing Materialize arguments: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Don't fail the task, just log the error

def run_pymonik_worker():
    """Run the worker."""

    @armonik_worker()
    def processor(task_handler: TaskHandler) -> Output:
        try:
            logger = ClefLogger.getLogger("ArmoniKWorker")
            logger.info("Starting PymoniK worker... Loading the payload")
            # Deserialize the payload
            payload = pickle.loads(task_handler.payload)
            func_name = payload["func_name"]
            func_id = payload["func_id"]
            require_context = payload["require_context"]
            args = payload["args"]
            requested_environment = payload["environment"]
            logger.info(
                f"Processing task {task_handler.task_id} : {func_name} -> {func_id} with arguments {args} in session {task_handler.session_id} "
            )
            # # Look up the function
            # if func_name not in self._registered_tasks:
            #     return Output(f"Function {func_name} not found")

            env = RuntimeEnvironment(logger)
            env.construct_environment(requested_environment)

            retrieved_args = args.get_args()
            logger.info(
                f"Retrieved args for task {task_handler.task_id} : {func_name} -> {func_id} :  {args} in session {task_handler.session_id} "
            )
            logger.debug(f"Retrieved args count: {len(retrieved_args)}")
            
            # Process arguments, retrieving results if needed
            processed_args = []
            for arg in retrieved_args:
                if isinstance(arg, str) and arg == "__no_input__":
                    # Skip NoInput arguments
                    continue
                elif isinstance(arg, str) and arg.startswith("__result_handle__"):
                    # Retrieve the result data
                    result_id = arg[len("__result_handle__") :]
                    result_data = task_handler.data_dependencies[result_id]
                    processed_args.append(pickle.loads(result_data))
                elif isinstance(arg, str) and arg.startswith("__multi_result_handle__"):
                    # Retrieve multiple result data
                    result_ids = arg[len("__multi_result_handle__") :].split(",")
                    processed_args.append(
                        [
                            pickle.loads(task_handler.data_dependencies[result_id])
                            for result_id in result_ids
                        ]
                    )
                else:
                    processed_args.append(arg)

            # Load the function
            func = pickle.loads(task_handler.data_dependencies[func_id])
            logger.info(
                f"Processing task {task_handler.task_id} : Retrieved function {func_name} from data dependencies"
            )

            # Process materialization BEFORE creating context for the function
            logger.info(f"About to process materialize args")
            _process_materialize_args(func_name, processed_args, task_handler, logger)
            logger.info(f"Finished processing materialize args")

            # Call the function with the arguments
            if require_context:
                # If the function requires context, pass the task handler
                context = PymonikContext(
                    task_handler, logger
                )  # TODO: create the context before and make enrich logs with task/function info
                processed_args = [context] + processed_args
            else:
                # Otherwise, just pass the arguments
                processed_args = processed_args
                
            pymonik_worker_client = Pymonik(is_worker=True)
            # TODO: support returning multiple results (I don't have a feel for how this can be done in practice and it's something to look into)
            pymonik_worker_client.create(
                task_handler=task_handler,
                expected_output=task_handler.expected_results[0],
            )
            with pymonik_worker_client:
                result = func(*processed_args)

            if isinstance(result, ResultHandle) or isinstance(
                result, MultiResultHandle
            ):
                # If the result is a ResultHandle or MultiResultHandle, then there is a delegation going on and we should not send the result
                return Output()
            # Serialize the result
            result_data = pickle.dumps(result)

            # Get the expected result ID
            result_id = task_handler.expected_results[0]

            # Send the result
            task_handler.send_results({result_id: result_data})

            return Output()

        except Exception as e:
            import traceback

            logger.error(
                f"Error processing task {task_handler.task_id} : {e}\n{traceback.format_exc()}"
            )
            return Output(f"Error processing task: {e}\n{traceback.format_exc()}")

    # Run the worker
    processor.run()