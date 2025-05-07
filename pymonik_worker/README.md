# PymoniK Worker

This is the default PymoniK worker that ships with ArmoniK and that is provided in the `aneoconsulting/harmonik_snake` Docker image. You're encouraged to edit this worker image and adapt it for your own use case. For instance, you might want to include additional Python packages, programs or files by default, or you might want to use a specific Python version that we do not build or support.    

## Building your own PymoniK worker

1. Create a `.python-version` file containing the exact Python version that you're working with in the `pymonik_worker/` directory. It'll be used by uv when building the docker image.

2. Run the docker build command from the root directory of the repo:

    ```
    docker build -t my_pymonik_worker -f pymonik_worker/Dockerfile .
    ```

