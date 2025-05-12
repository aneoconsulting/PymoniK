# Hello ArmoniK

- We'll be using `uv` as our Python project manager, so if you haven't installed it yet, follow the instructions [here](https://docs.astral.sh/uv/getting-started/installation/).

- Moreover, we assume that you have a partition in your Armonik cluster with the name `pymonik` and that is using a pymonik worker image. You can either build your own or use the pre-prepared one for Python 3.10.12 (Python 3.10 should work just as well): `ineedzesleep/harmonic_snake`. If your partition is named differently then you need to pass in the name of the partition to Pymonik.

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

with Pymonik(endpoint="localhost:5001", environment={"pip":["numpy"]}): #(1) 
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
    intermediate_result = add.map_invoke( #(1)
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
    partial_final_1 = sum_arrays.invoke(intermediate_result[:5]) #(1)
    partial_final_2 = sum_arrays(intermediate_result[5:].wait().get()) #(2)
    final_result = add(partial_final_1.wait().get(), partial_final_2) #(3)
    print(final_result)

```

1. I execute this part of the computation remotely using half of the results of the previous step, without retrieving them.
2. I retrieve the other half of the results and run this part of the computation locally. 
3. I retrieve partial_final_1 and add the results locally

## Anonymous Tasks

You can create tasks from lambda functions by directly creating a Task object, for instance: 

```py
add_task = Task(lambda a, b: a+b, func_name="add") 
add_task.map_invoke([(1,2), (1,3)])
```

!!! warning
    
    Please note that when creating anonymous tasks using lambda functions, it's imperative that you give it a name on your own. 


Anonymous tasks are particularly useful when you want to "armonikize" code from other Python packages. For instance:

```py
numpy_sum = Task(np.sum)
```
## Subtasking

Subtasking is an ArmoniK feature that allows you to dynamically change your task graph based mid-task execution. This is best illustrated with the following scenario. Say we've implemented a vector addition task as follows: 

```py
@task
def vec_add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a + b
```

One way to enhance this operation through subtasking is by making it so the `vec_add` task checks the size of the vectors to add. If the size is bigger than a certain threshold, then we can split the input into two parts and then invoke the same task for these smaller inputs. 

Here is a sample code for this (check `test_client/adaptive_vector_addition.py` for the full example)

```py
VECTOR_SIZE_THRESHOLD = 512

@task
def aggregate_results(result_1, result_2) -> np.ndarray:
    return np.concatenate([result_1, result_2])

@task
def vec_add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if a.size > VECTOR_SIZE_THRESHOLD:
        mid_point = a.size // 2 #(1)!
        a1, a2 = np.split(a, [mid_point])
        b1, b2 = np.split(b, [mid_point])

        result_handle1 = vec_add.invoke(a1, b1) #(2)!
        result_handle2 = vec_add.invoke(a2, b2)

        return aggregate_results.invoke(result_handle1, result_handle2, delegate=True) #(3)!
    else:
        return a + b #(4)!
```

1. Split vectors into two chunks, ideally you'd have a chunk size and you'd split into miltiple chunks. A half-way split was chosen here to highlight subtasking.
2. We invoke the `vec_add` task for each split. Note that you cannot wait or get there task results here. As Armonik's design philosophy is centered around workers being ephemeral. We can still invoke other tasks that make use of these results.
3. Aggregate the results using the aggregate_results task. We directly use the result handles from the sub-tasks that were invoked. The `delegate=True` basically tells ArmoniK that the result of vec_add will be the result of this task. So on the user side of things, you don't get a ResultHandle wrapped around a ResultHandle. This is sub-tasking. The result of the parent task will be set to the result of the delegated sub-task. 
4. If the vectors are of adequate size, we sum them up as usual and return their value.

There is another much simpler example of subtasking in `test_client/subtasking.py`


## Context

Sometimes, you might want to log messages from your tasks. To do that, you can add a `PymonikContext` to your task :

```py
@task(require_context=True)
def my_task(ctx):
    ctx.logger.info("This is an info log")
    ctx.logger.error("This is an error log", my_keyword="hello from pymonik") #(1)!
```

1. I can add additional information/metadata to display on Seq. 

You can also use the context to access the current environment, and in particular to install packages in that specific task. Although this isn't recommended as it will just cause environment contamination. It's preferred to have a single environment that you define from your PymoniK client. 


The context also gives you direct access to the task handler for that worker, if you ever feel the need to do more advanced work with the low level Python API for ArmoniK. (Not recommended)
## Storing objects in the cluster and reusing them

You might happen into scenarios where you'd like to store an object in your ArmoniK cluster and reuse it throughout. For that, you can `put` it into the ArmoniK cluster.

```py
from pymonik import ResultHandle

with Pymonik() as pymonik:
    df = pd.read_csv("some_data.csv") #(1)!
    df_handle: ResultHandle[pd.Dataframe] = pymonik.put(df) #(2)!
    some_operation.invoke(df_handle) #(3)!
    some_other_operation.invoke(df_handle)
```

1. Dataframe is read locally.
2. Dataframe is uploaded to the ArmoniK cluster, you get back a reusable handle that points to this remote object.
3. Invoke multiple operations on the ArmoniK cluster that reuse the same dataframe.

This is really useful for larger objects because it minimizes transfer time to the cluster, moreover, you might be able to benefit from worker level caching whenever it's implemented.   


!!! warning

    You're not required to do this for every object that you're dealing with, you can just pass everything into your tasks and ArmoniK will take care of everything; `pymonik.put` is just an additional optimization when you're reusing the same object over and over again (same object being passed over to multiple tasks). 
    If you end up modifying your object after the put then PymoniK will not synchronize these changes over to the workers. It's better to think of the sent objects as constants in that sense to avoid making mistakes.


There is also a `put_many` if you want to store multiple objects at the same time. (This is more efficient than looping through a list of objects and `put`-ing them individually).

You can also give your object a name, this makes it easy to see the objects you're putting when looking through the ArmoniK dashboard or if you want to search for it in the ArmoniK.CLI. 
## Connecting to ArmoniK

If you've deployed ArmoniK on your own, you should've been prompted to run a command for setting the `AKCONFIG` environment variable.

```sh
export AKCONFIG=...
```
This environment variables points to a config file that contains everything needed to connect to this cluster. If you set it before executing your client, then you can just write `pymonik=Pymonik()` and it'll connect automatically to the exported Armonik cluster. 

If you want to connect to multiple Armonik clusters, the invoke methods can accept a pymonik client argument. Which allows you to do something like:

```py
pymonik1 = Pymonik( """""" ) #(1)
pymonik2 = Pymonik( """""" ) #(2)

my_task.invoke(arg1, arg2, pymonik=pymonik1) #(3)
my_task.invoke(arg1, arg2, pymonik=pymonik2) #(4)
```

1. Specify the connection options and environment configuration for your first cluster.
2. Specify the connection options and environment configuration for your second cluster.
3. This task is invoked in the first cluster.
4. This task is invoked in the second cluster.
