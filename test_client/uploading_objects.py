from pymonik import Pymonik, task

@task
def add_one(x): 
    return x + 1

if __name__ == "__main__":
    pymonik = Pymonik(endpoint="localhost:5001", partition="pymonik")
    print("PymoniK client running..")
    with pymonik:
        try:
            ref = pymonik.put(41)
            result = add_one.invoke(ref).wait().get()
            print(f"Result of add_one task: {result}")
        except Exception as e:
            print(f"Error: {e}")
