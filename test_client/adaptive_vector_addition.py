import numpy as np
from pymonik import Pymonik, task

# Define a threshold for vector size.
VECTOR_SIZE_THRESHOLD = 512

@task
def aggregate_results(result_1, result_2) -> np.ndarray:
    return np.concatenate([result_1, result_2])

@task
def vec_add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Adds two numpy vectors. If vectors are larger than VECTOR_SIZE_THRESHOLD,
    it splits them into chunks and invokes itself as subtasks.
    The results are then aggregated by the aggregate_results task.
    """
    if not isinstance(a, np.ndarray) or not isinstance(b, np.ndarray):
        raise TypeError("Inputs must be numpy arrays.")

    if a.shape != b.shape:
        raise ValueError("Input vectors must have the same shape.")

    if a.size > VECTOR_SIZE_THRESHOLD:
        # Split vectors into two chunks, ideally you'd have a chunk size and you'd split into miltiple chunks.
        # A half-way split was chosen here to highlight subtasking.
        mid_point = a.size // 2
        a1, a2 = np.split(a, [mid_point])
        b1, b2 = np.split(b, [mid_point])

        # Invoke vec_add as subtasks for each chunk
        # Use delegate=True for subtasking as shown in the example
        result_handle1 = vec_add.invoke(a1, b1)
        result_handle2 = vec_add.invoke(a2, b2)

        # Aggregate the results using the aggregate_results task
        # Pass the result handles directly
        return aggregate_results.invoke(result_handle1, result_handle2, delegate=True)
    else:
        # If vectors are small enough, perform addition directly
        return a + b

if __name__ == "__main__":
    # Ensure you have an ArmoniK cluster running and accessible
    # Set endpoint and partition name accordingly
    # The environment needs numpy

    with Pymonik(endpoint="localhost:5001", partition="pymonik", environment={"pip": ["numpy"]}):
        # Create large sample vectors
        vector_size = 4096 # Example size larger than threshold
        vec_a = np.arange(vector_size)
        vec_b = np.arange(vector_size) * 2

        print(f"Invoking vec_add with vector size: {vector_size}")
        # Invoke the main task
        final_result_handle = vec_add.invoke(vec_a, vec_b)

        # Wait for the final result and retrieve it
        try:
            final_result = final_result_handle.wait().get()
            print(f"Result of vec_add task: {final_result}")
            print(f"Expected result starts with: {np.arange(vector_size) + np.arange(vector_size) * 2}")
            # Verify a small part of the result
            # print(f"First 10 elements: {final_result[:10]}")
            # print(f"Expected first 10: {(vec_a + vec_b)[:10]}")
            assert np.array_equal(final_result, vec_a + vec_b)
            print("Verification successful!")
        except Exception as e:
            print(f"An error occurred: {e}")

    print("PymoniK client finished.")
