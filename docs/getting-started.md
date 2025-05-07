# Hello ArmoniK

- We'll be using `uv` as our Python project manager, so if you haven't installed it yet, follow the instructions [here](https://docs.astral.sh/uv/getting-started/installation/).

- Moreover, we assume that you have a partition in your Armonik cluster with the name `pymonik` and that is using a pymonik worker image. You can either build your own or use the pre-prepared one for Python 3.10.12: `ineedzesleep/harmonic_snake`. If your partition is named differently then you need to pass in the name of the partition to Pymonik.

    ```py
    pymonik = Pymonik(partition="my_pymonik_partition")
    ```

## Creating a new project

```sh
mkdir hello_pymonik && cd hello_pymonik
uv init --python 3.10.12
```

Install the pymonik package

```
uv add pymonik
```

## Pymonik basics

It's best to learn by example

```py
from pymonik import Pymonik, task

@task #(1)
def add(a, b): #(2)
    return a + b

with Pymonik(endpoint="localhost:5001"): #(3)
    result = add.invoke("Hello", " World!").wait().get()
    print(result)
```

1. To create a task for ArmoniK, it suffices to use the `@task` decorator, if you're working with other decorators, make sure that `@task` is applied last
2. You can define your Python function as usual, you don't need to worry about anything. Just be aware that this is to be executed remotely.
3. Tasks invoked inside a Pymonik context will be executed in the Armonik cluster associated with said context.


This simple example basically creates an ArmoniK task to add up two strings. To execute a Python function on a remote cluster you `.invoke` it, passing in the same parameters you would've if you called it locally. To execute a Python function locally you can call it like you would have usually. 

At the end of a PymoniK call you get a handle to the execution result. I can at any point block my execution to `wait` for a certain result or continue executing my code. To wait for a result, you call the `wait` method. Note however that the wait method does not return the actual value of the result. To do so, you'll need to call `get`. `get` will download the execution result to your local machine. 

In essence, you'll be working with result handles throughout your Pymonik programs. Let's add a new task to our previous code to multiply two numbers.

```py
@task 
def add(a, b):
    return a + b

@task
def multiply(a,b):
    return a*b

with Pymonik(endpoint="localhost:5001"):
    intermediate_result = add.invoke(2, 3) #(1)
    final_result = multiply.invoke(intermediate_result, 5).wait().get()
    print(final_result)
```

1. We don't need to block and wait for the result of the addition, we can just pass the ResultHandle to multiply task and it'll execute when this result is ready.


Running this code should yield the result `25`. This isn't really exciting though, let's try running some operations on arrays, we'll be using numpy. 

First, let's install numpy locally: 

```sh
uv add numpy
```

```py
from pymonik import Pymonik, task
import numpy as np

@task 
def add(a, b):
    return a + b

@task
def multiply(a,b):
    return a*b

with Pymonik(endpoint="localhost:5001", environment={"pip":["numpy"]}): # (1) 
    intermediate_result = add.invoke(np.array([1,2,3]), np.array([2,1,0])) 
    final_result = multiply.invoke(intermediate_result, 2).wait().get()
    print(final_result)
```

1. To specify a global execution environment for all your tasks, you just need to add an environment argument

You can pass in a list of packages to install in your remote workers via the environment argument to the Pymonik client. If you'd like to specify specific versions, you just need to pass in a tuple with `(package_name, version_specifier)`. This example should yield the result `[6 6 6]`.

The environment argument also supports the "env_variables" key which allows you to pass in a dictionary with environment variables to set on the worker.

Now let's take this another notch and invoke multiple tasks in parallel. To do this, we use `map_invoke`. 

```py
from pymonik import Pymonik, task
import numpy as np

@task 
def add(a, b):
    return a + b

@task
def sum_arrays(arrays):
    return np.sum(arrays)

with Pymonik(endpoint="localhost:5001", environment={"pip":["numpy"]}):
    intermediate_result = add.map_invoke( # (1)
        [(np.random.randint(0,10, size=(3,3)) ,np.random.randint(0,10, size=(3,3))) for _ in range(10)]
    ) 
    final_result = sum_arrays.invoke(intermediate_result).wait().get()
    print(final_result)

```

1. We pass in a list of the arguments that we'd like to execute remotely. You should provide the same arguments that a function expects in the form of a tuple. A `map_invoke` returns a MultiResultHandle.

`map_invoke` allows you to submit multiple tasks to your cluster in parallel and returns a MultiResultHandle. If you `wait` here then it'll wait until all results ready. 

A MultiResultHandle behaves like a list, this allows you to selectively wait for results or split the computation. 

I can for instance write:

```py
from pymonik import Pymonik, task
import numpy as np

@task 
def add(a, b):
    return a + b

@task
def sum_arrays(arrays):
    return np.sum(arrays)

with Pymonik(endpoint="localhost:5001", environment={"pip":["numpy"]}):
    intermediate_result = add.map_invoke( 
        [(np.random.randint(0,10, size=(3,3)) ,np.random.randint(0,10, size=(3,3))) for _ in range(10)]
    ) 
    partial_final_1 = sum_arrays.invoke(intermediate_result[:5]) # (1)
    partial_final_2 = sum_arrays(intermediate_result[5:].wait().get()) # (2)
    final_result = add(partial_final_1.wait().get(), partial_final_2) # (3)
    print(final_result)

```

1. I execute this part of the computation remotely using half of the results of the previous step, without retrieving them.
2. I retrieve the other half of the results and run this part of the computation locally. 
3. I retrieve partial_final_1 and add the results locally


## Subtasking

Subtasking is an ArmoniK feature that allows you to dynamically change your task graph based mid-task execution. This is best illustrated with the following scenario. Say we've implemented a vector addition task as follows: 

```py
@task
def vec_add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a + b
```

## Storing objects in the cluster and reusing them

You might happen into scenarios where you'd like to store an object in your ArmoniK cluster and reuse it throughout. For that, you can `put_object` it 

```py
df = pd.read_csv("some_data.csv") # (1)
df_handle = put_object(df) # (2)
some_operation.invoke(df_handle) # (3)
some_other_operation.invoke(df_handle)
```

1. Dataframe is read locally.
2. Dataframe is uploaded to the ArmoniK cluster, you get back a reusable handle that points to this remote object.
3. Invoke multiple operations on the ArmoniK cluster that reuse the same dataframe.

This is really useful for larger objects because it minimizes transfer time to the cluster, moreover, you might be able to benefit from worker level caching whenever it's implemented.   

## Connecting to ArmoniK

If you've deployed ArmoniK on your own, you should've been prompted to run a command for setting the `AKCONFIG` environment variable.

```sh
export AKCONFIG=...
```
This environment variables points to a config file that contains everything needed to connect to this cluster. If you set it before executing your client, then you can just write `pymonik=Pymonik()` and it'll connect automatically to the exported Armonik cluster. 

If you want to connect to multiple Armonik clusters, the invoke methods can accept a pymonik client argument. Which allows you to do something like:

```py
pymonik1 = Pymonik( """""" ) # (1)
pymonik2 = Pymonik( """""" ) # (2)

my_task.invoke(arg1, arg2, pymonik=pymonik1) # (3)
my_task.invoke(arg1, arg2, pymonik=pymonik2) # (4)
```

1. Specify the connection options and environment configuration for your first cluster.
2. Specify the connection options and environment configuration for your second cluster.
3. This task is invoked in the first cluster.
4. This task is invoked in the second cluster.
