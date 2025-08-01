from time import sleep
from pymonik import Pymonik, PymonikContext, task
import cloudpickle as pickle
import os

def find_file(filename, search_path=None):
    """
    Search for a file by name in the file system.
    
    Args:
        filename (str): Name of the file to search for
        search_path (str, optional): Root directory to start search from. 
                                   Defaults to current working directory.
    
    Returns:
        str or None: Full path to the file if found, None if not found
    """
    if search_path is None:
        search_path = os.getcwd()
    
    for root, dirs, files in os.walk(search_path):
        if filename in files:
            return os.path.join(root, filename)
    
    return None

@task(require_context=True)
def my_task(ctx: PymonikContext, result_id):
    
    if ctx.retrieve_object(result_id):
        ctx.logger.info("Found the object !")
        path = find_file(result_id, "/")
        ctx.logger.info(f"GetObjectPath({result_id}) == {ctx.get_object_path(result_id)}")
        ctx.logger.info(f"find_file({result_id}) == {path}")
        with open(str(path), "rb") as fh:
            contents = fh.read()
            unpickled_contents = pickle.loads(contents)
            ctx.logger.info(f"File contents (binary) = {contents}")
            ctx.logger.info(f"File contents (unpickled) = {unpickled_contents}")
        return path == str(ctx.get_object_path(result_id))
    else:
        ctx.logger.info("Couldn't find the object !")
        return None


if __name__ == "__main__":
    with Pymonik("localhost:5001") as pk:
        handle = pk.put("H"*100, "my_obj").wait()
        print(f"Result id = {handle}")
        res = my_task.invoke(handle.result_id).wait().get()
        print(res)
