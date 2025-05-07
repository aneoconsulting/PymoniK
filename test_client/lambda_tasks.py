from pymonik import Pymonik, Task

if __name__ == "__main__":
    pymonik = Pymonik(endpoint="localhost:5001", partition="pymonik")
    print("PymoniK client running..")
    with pymonik:
        try:
            my_task = Task(lambda a, b: a+b, func_name="add")
            result = my_task.invoke(1, 2).wait().get()
            print(f"Result of add task: {result}")
        except Exception as e:
            print(f"Error: {e}")
