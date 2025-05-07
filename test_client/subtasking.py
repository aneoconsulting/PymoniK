from pymonik import Pymonik, task

@task 
def add_one(a:int) -> int:
    """
    A simple task that adds one to an integer.
    """
    return a + 1

@task
def add(a: int, b: int) -> int:
    """
    A simple task that adds two integers.
    """
    if a <= 0:
        return b
    b_plus_one = add_one.invoke(b)
    return add.invoke(a-1, b_plus_one, delegate=True) 


if __name__ == "__main__":
    pymonik = Pymonik(endpoint="localhost:5001", partition="pymonik")
    print("PymoniK client running..")
    with pymonik:
        try:
            result = add.invoke(8, 2).wait().get()
            print(f"Result of add task: {result}")
        except Exception as e:
            print(f"Error: {e}")
