# Creating your own PymoniK worker

It's pretty simple to create your own ArmoniK worker, you can start by modifying the pymonik_worker image. In terms of code you just need to call the `run_pymonik_worker` method. 

```py
from pymonik import run_pymonik_worker

run_pymonik_worker()
```

But other than that, you're free to do anything in the worker. You can create your own ArmoniK worker image by using the `pymonik_worker`'s as a starting point. The most important part is properly configuring the armonikuser:

```Dockerfile
RUN groupadd --gid 5000 armonikuser && \
    useradd --home-dir /home/armonikuser --create-home --uid 5000 --gid 5000 --shell /bin/sh --skel /dev/null armonikuser && \
    mkdir /cache && \
    chown armonikuser: /cache && \
    chown -R armonikuser: /app 

USER armonikuser
```
