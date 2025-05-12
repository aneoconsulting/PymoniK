## PymoniK


PymoniK is a dead simple Python framework for writing distributed programs that run on an ArmoniK cluster. It's a wrapper around the low-level `ArmoniK.API` that allows you to easily make your Python programs distributed. 

- Make your functions run in the cloud using a simple decorator.

```
@task
def hello_worlder():
    return "hello world"

with Pymonik():
    print(hello_worlder.invoke().wait().get())
```

- Run multiple tasks in parallel:

```py
@task
def add(a,b):
    return a+b

with Pymonik():
    results = add.map_invoke([(i, i+1) for i in range(32)])
    print(results.wait().get())

```

- Easily construct and run complex task graphs, interweave local and remote code execution:

```py
@task
def get_constant()
    return 2

@task
def add(a,b):
    return a+b

with PymoniK():
    my_constant = get_constant()
    results = add.map_invoke([(my_constant(), i) for i in range(32)])
    sum_task = Task(sum)
    remote_partial_result = sum_task.invoke(results[:16])
    local_partial_result = sum_task(results[16:].wait().get())
    final_result = remote_partial_result.wait().get() + local_partial_result
    print(final_result)
```

- Define your remote execution environment (specify Python packages), subtasking and more.

If you're interested in using PymoniK, please take a look at our [getting started guide](getting-started.md).

