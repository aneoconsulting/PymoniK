# Multi-partition routing

A single PymoniK session can submit tasks to more than one partition.
This is how you mix CPU and GPU work in one logical workflow without
opening two clients.

## Single partition (the default)

```python
with client.session(partition="pymonik") as s:
    add.spawn(2, 3).result()
```

The session is bound to one partition. Any task that tries to route
elsewhere (`@task(partition="gpu")`) fails at submit time with a
`PymonikError`:

```text
task 'render' requested partition 'gpu', but the session is only bound to ['pymonik'].
Pass that partition to client.session(partition=[...]) to enable it.
```

## Multiple partitions on one session

Pass a list:

```python
with client.session(partition=["cpu", "gpu", "io"]) as s:
    ...
```

- The **first** partition is the default for tasks that don't pick
  one explicitly.
- The full list is what the session advertises to ArmoniK on create.
- Per-task partition selection (`@task(partition="gpu")` or
  `.with_options(partition="gpu")`) must be one of the declared
  partitions.

```python
@task
def cheap(x): return x

@task(partition="gpu")
def render(scene): ...

with client.session(partition=["cpu", "gpu"]) as s:
    cheap.spawn(1)              # routes to "cpu" (default)
    render.spawn(scene)         # routes to "gpu" (explicit)

    fast = render.with_options(partition="gpu-a100")  # NOT in the set
    fast.spawn(scene)            # raises PymonikError at submit time
```

## When to use this

- **Tasks need different hardware.** GPU tasks on a GPU partition, CPU
  pre/post-processing on a CPU partition, all stitched together with
  data dependencies.
- **Different worker images per route.** Partition A runs an image
  with TensorFlow; partition B runs one with PyTorch. Both bound to
  the session, tasks pick at submit time.
- **Quota or priority isolation.** Some operators put noisy
  experiments on a separate partition and route only specific tasks
  there.

## When not to use this

- **One partition is fine.** Don't list multiple if you don't need
  them; the validation cost is real (every submission checks partition
  membership), and the cluster sees a session it can route to N
  partitions even if you only ever use one.
- **Cross-cluster routing.** A session is bound to one cluster. To
  submit work to multiple clusters, open multiple `PymonikClient`
  instances.

## Inspecting a session's partitions

Both attributes are available on a `Session`:

```python
with client.session(partition=["cpu", "gpu"]) as s:
    s.partition       # "cpu"  — the default
    s.partitions      # ("cpu", "gpu")  — the full set
```

## Discovering what's available

The cluster's partition catalogue is reachable from the client (no
session needed):

```python
with PymonikClient() as client:
    for p in client.partitions.list():
        print(p.id, p.priority, p.preemption_percentage)
```

Use this to decide what to bind in `client.session(partition=[...])`,
or to script a "give me the lowest-priority partition with a free
slot" allocation.
