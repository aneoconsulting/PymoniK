# Monte Carlo: estimating π

A short example that hits every basic primitive: a worker function,
`map` for fan-out, `spawn` for fan-in.

The full source lives at `examples/estimate_pi.py`.

## The idea

Monte Carlo estimation of π: throw N random points in the unit square;
the fraction inside the unit quarter-circle is approximately π/4. The
more points, the better the estimate.

It's embarrassingly parallel — every chunk of points is independent,
and the only fan-in is a sum. Perfect for ArmoniK.

## The code

```python
import random
from pymonik import PymonikClient, task


@task
def count_inside(n: int, seed: int) -> int:
    """How many of `n` random points fall inside the unit quarter circle?"""
    rng = random.Random(seed)
    inside = 0
    for _ in range(n):
        x = rng.random()
        y = rng.random()
        if x * x + y * y <= 1.0:
            inside += 1
    return inside


@task
def estimate(total_inside: int, total_points: int) -> float:
    """Combine the per-shard counts into a single π estimate."""
    return 4.0 * total_inside / total_points


@task
def add_all(xs: list[int]) -> int:
    return sum(xs)


def run(total_points: int = 10_000_000, num_tasks: int = 32) -> float:
    points_per_task = total_points // num_tasks

    with PymonikClient() as client:
        with client.session(partition="pymonik") as s:
            shards = count_inside.starmap(
                (points_per_task, i) for i in range(num_tasks)
            )
            total_inside = add_all.spawn(shards)
            pi = estimate.spawn(total_inside, num_tasks * points_per_task)
            return pi.result(timeout=120)


if __name__ == "__main__":
    print(f"π ≈ {run()}")
```

## What's happening

- `count_inside.starmap(...)` submits 32 tasks in one gRPC call. Each
  one runs in parallel on whatever workers ArmoniK schedules. Returns
  a `FutureList[int]`. We use `starmap` because we already have arg
  tuples; if the per-task args were single values, `count_inside.map(iter)`
  would be cleaner.
- `add_all.spawn(shards)` passes the `FutureList` directly. PymoniK
  rewrites each upstream future as a data dependency — `add_all` won't
  run until every `count_inside` has finished. The client doesn't
  block.
- `estimate.spawn(total_inside, ...)` chains again: `estimate` waits
  for `add_all` via the same mechanism.
- Only `pi.result(timeout=120)` blocks. By the time the client wakes
  up, the entire DAG (32 + 1 + 1 = 34 tasks) has run.

## Tweaking

- **More accuracy?** Bigger `total_points`. Each shard is independent,
  so increasing the count just makes the leaves heavier.
- **More parallelism?** Bigger `num_tasks`. Each shard is small enough
  that the overhead of submission per task starts to matter at a few
  hundred — you'll see diminishing returns.
- **Reproducible?** Drop the seed indirection and pass a fixed seed.
  The worker uses Python's stdlib `random`, which is deterministic
  given a seed.

## Variations worth trying

1. Replace `count_inside` with one that uses `numpy.random` for
   speed — declare `client.session(deps=["numpy"])` to make numpy
   available without rebuilding the image. See
   [Runtime environment](../guides/runtime-environment.md).
2. Use the local exec cache to skip re-running shards:
   `PymonikClient(cache=True)` + `@task(cache=True)` on
   `count_inside`. Re-running the script with the same args returns
   instantly on the second invocation.
3. Use `LocalCluster` to run the whole thing in-process for a unit
   test — no cluster needed. See
   [Local testing](../guides/local-testing.md).
