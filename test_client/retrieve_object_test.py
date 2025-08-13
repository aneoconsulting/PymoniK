
from pymonik import Pymonik, PymonikContext, task

@task(require_context=True)
def test_retrieve_with_unpickling(ctx: PymonikContext, result_id: str):
    """Test retrieving and automatically unpickling an object."""
    ctx.logger.info(f"Testing retrieve_object with auto_unpickle=True for {result_id}")
    
    # This will retrieve and unpickle the object automatically
    obj = ctx.retrieve_object(result_id)
    
    if obj is not None:
        ctx.logger.info(f"Successfully retrieved and unpickled object: {obj}")
        ctx.logger.info(f"Object type: {type(obj)}")
        return f"Success: {obj}"
    else:
        ctx.logger.error("Failed to retrieve/unpickle object")
        return "Failed"

@task(require_context=True) 
def test_retrieve_without_unpickling(ctx: PymonikContext, result_id: str):
    """Test retrieving an object without unpickling."""
    ctx.logger.info(f"Testing retrieve_object with auto_unpickle=False for {result_id}")
    
    # Just retrieve the file, don't unpickle
    success = ctx.retrieve_object(result_id, auto_unpickle=False)
    
    if success:
        object_path = ctx.get_object_path(result_id)
        ctx.logger.info(f"Successfully retrieved object to {object_path}")
        
        # Manually check file size
        file_size = object_path.stat().st_size
        ctx.logger.info(f"Retrieved file size: {file_size} bytes")
        return f"Success: Retrieved {file_size} bytes to {object_path}"
    else:
        ctx.logger.error("Failed to retrieve object")
        return "Failed"

@task(require_context=True)
def test_check_exists(ctx: PymonikContext, result_id: str):
    """Test the check_exists and force_retrieve functionality."""
    ctx.logger.info(f"Testing existence checking for {result_id}")
    
    # Check if object exists locally first
    exists_initially = ctx.object_exists_locally(result_id)
    ctx.logger.info(f"Object exists locally initially: {exists_initially}")
    
    # First retrieval (should actually retrieve from ArmoniK)
    obj1 = ctx.retrieve_object(result_id, check_exists=True)
    ctx.logger.info(f"First retrieval result: {obj1}")
    
    # Second retrieval (should use local copy)  
    obj2 = ctx.retrieve_object(result_id, check_exists=True)
    ctx.logger.info(f"Second retrieval result (should be from cache): {obj2}")
    
    # Force retrieval (should retrieve from ArmoniK even though local copy exists)
    obj3 = ctx.retrieve_object(result_id, check_exists=True, force_retrieve=True)
    ctx.logger.info(f"Force retrieval result: {obj3}")
    
    return {
        "existed_initially": exists_initially,
        "first_retrieval": str(obj1),
        "second_retrieval": str(obj2), 
        "force_retrieval": str(obj3),
        "all_match": obj1 == obj2 == obj3
    }

if __name__ == "__main__":
    with Pymonik("localhost:5001") as pk:
        # Create test objects
        simple_obj = "Hello PymoniK!"
        complex_obj = {
            "data": list(range(100)),
            "metadata": {"created_by": "test", "version": 1.0},
            "nested": {"deep": {"value": 42}}
        }
        
        # Upload objects to ArmoniK
        simple_handle = pk.put(simple_obj, "simple_test_obj")
        complex_handle = pk.put(complex_obj, "complex_test_obj")
        
        print(f"Simple object result ID: {simple_handle.result_id}")
        print(f"Complex object result ID: {complex_handle.result_id}")
        
        # Test 1: Basic retrieve with unpickling
        print("\n=== Test 1: Retrieve with unpickling ===")
        result1 = test_retrieve_with_unpickling.invoke(simple_handle.result_id).wait().get()
        print(f"Result: {result1}")
        
        # Test 2: Retrieve without unpickling
        print("\n=== Test 2: Retrieve without unpickling ===")
        result2 = test_retrieve_without_unpickling.invoke(complex_handle.result_id).wait().get()
        print(f"Result: {result2}")
        
        # Test 3: Test existence checking and caching
        print("\n=== Test 3: Existence checking and caching ===")
        result3 = test_check_exists.invoke(simple_handle.result_id).wait().get()
        print(f"Result: {result3}")
        
        print("\n=== All tests completed ===")
