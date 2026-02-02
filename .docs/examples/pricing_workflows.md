# Pricing Workflows with Pymonik

This document provides a detailed overview of two prevalent pricing workflows that are constructed using [**ArmoniK**](https://github.com/aneoconsulting/ArmoniK), a hybrid framework designed to simplify the development of distributed applications, particularly in high-performance computing (HPC) and High Throughput environments, and
[**PymoniK**](https://github.com/aneoconsulting/PymoniK), a Python framework designed to interface with ArmoniK seamlessly.

## Pricing Workflow Scenarios

The document specifically covers two distinct scenarios in order to illustrate the usage and versatility of these tools:

* **Scenario 1** – This scenario focuses on a straightforward, synchronous pricing workflow. It is designed to illustrate how pricing tasks can be executed in a linear fashion, with each step waiting for the previous one to complete before moving forward.

* **Scenario 2** – In contrast, this scenario showcases a more advanced and scalable pricing workflow that incorporates subtasking and dynamic task graphs. This approach allows for a more flexible execution of pricing tasks, enabling the system to adapt to varying demands by breaking down tasks into smaller subtasks that can run concurrently.

### Breakdown of Each Scenario

For both scenarios, the document systematically explains several key aspects:

* **End-to-End Workflow** – We provide an overview of the entire workflow from the user's point of view. This includes each interaction the user has with the system, detailing how the pricing requests are submitted and processed sequentially or concurrently.

* **ArmoniK’s Internal Functions** – An in-depth look at what happens behind the scenes within ArmoniK during the workflow execution. This includes explanations of how tasks are scheduled, resources are managed, and results are compiled to ensure efficient processing.

* **Implementation in Python** – A practical guide on how to implement each workflow scenario using PymoniK within a Python environment. This section offers code snippets and explanations to help users understand how to leverage the library effectively for their pricing needs.

### Prerequisites for Understanding the Examples

The examples provided throughout the document are based on the following assumptions :

* **ArmoniK Cluster** – It is expected that the reader has access to an operational ArmoniK cluster, which serves as the foundation for running distributed pricing tasks.

* **Python-Based Worker Image** – The document assumes that users are working with a worker image that is compatible with Python, ensuring that the examples can be executed without compatibility issues.

* **Basic Knowledge of PymoniK** – A fundamental understanding of PymoniK’s tasks, how to invoke them, and how to handle results is assumed. This prior knowledge will enable readers to follow the implementation steps more effectively.


---

## Scenario 1 – Simple Pricing Workflow

This scenario illustrates the a basic and direct pricing interaction pattern supported by ArmoniK.
It is intentionally minimal and synchronous, so it is straighforward to understand and ideal as a
starting point for new users. In this workflow:

1. The user prepares all required input data locally, including:
   * Market data (e.g., spot prices, rates, volatilities)
   * Product or instrument definitions (e.g., an option with a notional)
   * Any additional pricing parameters
2. The user submits a **single pricing task** to ArmoniK.
3. The user waits synchronously for the task to complete.
4. Once execution finishes, the pricing result is retrieved and returned to the user.

There is no task decomposition, no fan-out/fan-in logic, and no dependency management.
The entire pricing request is handled as one atomic unit of work.

This interaction model is particularly well suited for:

* Pricing a single financial instrument
* Lightweight or fast pricing models
* Interactive workflows (e.g., notebooks, scripts, UI-driven tools)
* Situations where immediate feedback is required

---

### Workflow Diagram

The diagram below shows the logical flow of data and control between the user, ArmoniK, and the pricing function.


```mermaid
graph TD
    %% Define other nodes
    id1["Portfolio"]
    id2["Market Data"]
    id3((("user")))
    id4["pricer"]
    id5["Final Portfolio Price"]

    %% Define connections
    id1 --> id4
    id2 --> id4
    id3 -- "1: User provides input data" --> id1
    id3 -- "2: User submits the task" --> id4
    id4 --> id5
    id3 -- "3: User waits for the result availability and downloads the result" --> id5

```

Hence:

- The user is responsible for assembling the input data (portfolio definition and market data).
- The pricing task consumes these inputs and performs the computation.
- ArmoniK executes the task remotely and produces a final portfolio price.
- The user explicitly waits for the computation to complete and then retrieves the result.

### What ArmoniK Does

From ArmoniK’s point of view, this scenario follows a simple and linear execution path:

- Receives a single pricing task submission from the client
- Places the task in the scheduler queue
- Assigns the task to an available worker node
- Executes the pricing function in a distributed environment
- Persists the final result in the distributed result store
- Makes the result available for download by the client

Because the task is fully self-contained:

- No dynamic task graph is created
- No subtasks are generated
- No dependency resolution is required
- No intermediate results are exposed

### Example Code

The following example demonstrates how to define and invoke a simple pricing task using PymoniK.

```{code-block} python
:linenos:
from pymonik import Pymonik, task

# A simple pricing task
@task
def price_vanilla(option, market_data):
    # Simplified pricing logic
    return option["notional"] * market_data["spot"] * 0.01

# User workflow
with Pymonik(endpoint="localhost:5001"):
    option = {"notional": 1_000_000}
    market_data = {"spot": 105.0}

    result = price_vanilla.invoke(option, market_data).wait().get()
    print("Price:", result)
```

#### Step-by-Step Explanation

##### Task definition:

* Line 1 imports the ArmoniK client (Pymonik) and the `@task` decorator.
* Line 4 marks `price_vanilla` as a remotely executable ArmoniK task.
* Lines 5–7 define the pricing logic. All required inputs are passed as arguments, making the task fully self-contained and serializable.

##### User workflow:

* Line 10 opens a connection to the ArmoniK control plane using a context manager. Here we assume that the cluster is listening on `localhost` at port `5001`.
* Lines 11-12 define the product data and market data locally on the client.
* Line 14 submits the task for execution, waits synchronously for completion, and retrieves the result.
* Line 15 outputs the final price to the user.

From the user’s perspective, the call behaves much like a local function call, while ArmoniK transparently handles remote execution and scheduling.

### Key Characteristics

* One task → one result
* The user explicitly waits for completion
* Minimal orchestration logic
* Suitable for straightforward pricing problems

---

## Scenario 2 – Portfolio Pricing with Subtasking and Monte Carlo

### Overview

In this scenario, the pricing logic itself becomes responsible for **building and executing a task graph** dynamically.

The workflow is:

1. User provides a portfolio and market data
2. User submits a single *portfolio pricer* task
3. The pricer:

   * Prices all vanilla products
   * Builds a dynamic computation graph for complex products using Monte Carlo
   * Aggregates partial results
   * Aggregates the total portfolio value
4. The final result is **delegated** to the last aggregation task
5. The user retrieves a single portfolio-level result

This model is ideal for:

* Large portfolios
* Heterogeneous products
* Computationally intensive models (e.g. Monte Carlo)

### Workflow diagram

```mermaid

flowchart TB
    subgraph Inputs [" "]
        style Inputs fill:#ffffff, stroke:none;
        direction TB
        id1["Portfolio"]
        id2["Market Data"]
        id3["Pricer"]

        id1 --> id3
        id2 --> id3
    end

    id4(("User"))
    id4 -- "1: User provides input data" --> id1
    id4 -- "2: User submits the task" --> id3

    subgraph Subtasks [" "]
        style Subtasks fill:#ffffff, stroke:ffffff;
        direction TB
        v["Vanilla"]
        x["Complex Product 1"]
        y["Complex Product 2"]
        z["Complex Product 3"]

        xc1[" "]
        xc2[" "]
        xc3[" "]

        yc1[" "]
        yc2[" "]
        yc3[" "]

        zc1[" "]
        zc2[" "]
        zc3[" "]


        id2 --> xc1
        id2 --> xc2
        id2 --> xc3
        x --> xc1
        x --> xc2
        x --> xc3

        id2 --> yc1
        id2 --> yc2
        id2 --> yc3
        y --> yc1
        y --> yc2
        y --> yc3

        id2 --> zc1
        id2 --> zc2
        id2 --> zc3
        z --> zc1
        z --> zc2
        z --> zc3

        xd1[" "]
        xd2[" "]
        xd3[" "]

        xc1 --> xd1
        xc2 --> xd2
        xc3 --> xd3

        yd1[" "]
        yd2[" "]
        yd3[" "]

        yc1 --> yd1
        yc2 --> yd2
        yc3 --> yd3

        zd1[" "]
        zd2[" "]
        zd3[" "]

        zc1 --> zd1
        zc2 --> zd2
        zc3 --> zd3

        xa["Aggregate"]
        ya["Aggregate"]
        za["Aggregate"]

        xd1 --> xa
        xd2 --> xa
        xd3 --> xa

        yd1 --> ya
        yd2 --> ya
        yd3 --> ya

        zd1 --> za
        zd2 --> za
        zd3 --> za


        xr["Product Price"]
        xa --> xr

        yr["Product Price"]
        ya --> yr

        zr["Product Price"]
        za --> zr

        pa["Aggregate Portfolio"]

        v --> pa
        xr --> pa
        yr --> pa
        zr --> pa
    end

    id3 -- "4: The pricer submits a graph for each complex product and the result of all vanilla products" --> Subtasks

    id5["Final Portfolio Price"]

    id4 -- "3: User waits for result availability and downloads the result" --> id5
    pa --> id5

```

### What ArmoniK Does

* Executes the initial portfolio task
* Accepts **new task submissions from within running tasks** (subtasking)
* Dynamically extends the task graph
* Ensures dependencies are respected
* Propagates delegated results so that the parent task’s result becomes the final aggregation output

From the user’s point of view, this still looks like a **single task invocation**.

---

## Example: Portfolio Pricer with Subtasking

### Supporting Tasks

```python
import numpy as np
from pymonik import task

@task
def price_vanilla(option, market_data):
    return option["notional"] * market_data["spot"] * 0.01

@task
def mc_path(product, market_data, seed):
    rng = np.random.default_rng(seed)
    paths = rng.normal(market_data["spot"], 1.0, size=10_000)
    return np.mean(paths) * product["notional"]

@task
def aggregate_mc_results(results):
    return np.mean(results)

@task
def aggregate_portfolio(values):
    return sum(values)
```

### Complex Product Pricing via Subtasking

```python
@task
def price_complex_product(product, market_data):
    # Launch Monte Carlo paths in parallel
    mc_results = mc_path.map_invoke([
        (product, market_data, seed) for seed in range(16)
    ])

    # Delegate final product price to aggregation
    return aggregate_mc_results.invoke(mc_results, delegate=True)
```

### Portfolio Pricer (Entry Point)

```python
@task
def price_portfolio(portfolio, market_data):
    vanilla_products = [p for p in portfolio if p["type"] == "vanilla"]
    complex_products = [p for p in portfolio if p["type"] == "complex"]

    vanilla_prices = price_vanilla.map_invoke([
        (p, market_data) for p in vanilla_products
    ])

    complex_prices = price_complex_product.map_invoke([
        (p, market_data) for p in complex_products
    ])

    all_prices = vanilla_prices + complex_prices

    # Delegate final portfolio result
    return aggregate_portfolio.invoke(all_prices, delegate=True)
```

### User Code

```python
from pymonik import Pymonik

portfolio = [
    {"type": "vanilla", "notional": 1_000_000},
    {"type": "complex", "notional": 500_000},
]

market_data = {"spot": 100.0}

with Pymonik(endpoint="localhost:5001", environment={"pip": ["numpy"]}):
    result = price_portfolio.invoke(portfolio, market_data).wait().get()
    print("Portfolio value:", result)
```

---

## Summary

* **Scenario 1** demonstrates a straightforward request–response pricing model
* **Scenario 2** leverages ArmoniK’s dynamic task graph and subtasking capabilities to scale complex portfolio pricing
* Pymonik allows both workflows to be expressed naturally in Python while keeping the user-facing API simple

From a user’s perspective, both scenarios boil down to:

```python
result = some_pricer.invoke(...).wait().get()
```

The difference lies entirely in how much intelligence and orchestration is embedded inside the tasks themselves.
