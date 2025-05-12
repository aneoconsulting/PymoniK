# Developing PymoniK

We'll be covering some basic information to help you in working on and developing PymoniK

## Requirements

We're using `uv` throughout the project, so please make sure that you have it installed. You can refer to their [official `uv` installation guide](https://docs.astral.sh/uv/getting-started/installation/) 

## Test client

The test client contains some basic examples of working with PymoniK, PymoniK is installed in editable mode `uv add ../pymonik --editable`, it's useful to just create a python file there for testing and then `uv run`ning it to quickly iterate on PymoniK. Keep in mind that if you make changes that affect how the worker functions (obviously like making a change to the `worker.py` file), you'll have to reload the worker image. You can do this by running the following command:

```bash
kubectl rollout restart deployment/compute-plane-pymonik #(1) -n armonik #(2)
```

1. This should be compute-plane-(NAME OF YOUR PYMONIK PARTITION). 
2. If you're deploying locally the namespace is typically armonik, otherwise use the namespace of your kubernetes cluster


## Automation Script (`automation.py`)

The `automation.py` script at the root of the project should help you realize most of your development tasks, it also auto-installs development dependencies.

For example, if you want to access the documentation offline of if you're working on it (thank you!) then you can use the `serve-docs` command.

To see a list of all available commands and their general descriptions, you can run: 

```
uv run automation.py --help
```
