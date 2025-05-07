# PymoniK Worker

This is the default PymoniK worker that ships with ArmoniK and that is provided in the `aneoconsulting/harmonik_snake` Docker image. You're encouraged to edit this worker image and adapt it for your own use case. For instance, you might want to include additional Python packages, programs or files by default, or you might want to use a specific Python version that we do not build or support. You can refer to our guide in the docs for creating custom workers.

## Building your own PymoniK worker

From the root of the project, run: 
```
uv run automation.py build-docker
```

You can append `--help` to get additional options (such as refreshing the worker image used in your armonik deployment, setting the tag, python version, etc.)
