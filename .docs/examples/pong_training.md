# Distributed reinforcement learning (Pong)

Reinforcement learning workloads benefit from cluster compute when
the bottleneck is **rollout collection** — running game episodes to
gather experience for a learner. Each episode is independent; you
can run dozens or hundreds in parallel on cluster workers, then ship
trajectories back to a single learner.

This example uses ArmoniK to parallelise the rollout step of training
a pong-playing agent. The full source lives at
`examples/pong_training.py`.

## Approach

1. The client owns the model weights and the optimizer.
2. Each iteration: fan out N rollout tasks. Each task plays a few
   episodes against the current weights and returns trajectories.
3. The client collects trajectories, runs a gradient step, updates
   the weights, repeats.

The worker doesn't need to know about training — it only needs to
play. The client handles all gradient bookkeeping.

## Sketch

```python
from pymonik import PymonikClient, task
import pymonik.blob as blob

@task
def collect_rollouts(weights_blob, episodes_per_task: int, seed: int) -> dict:
    """Play `episodes_per_task` episodes; return trajectories.

    `weights_blob` is a Blob[bytes] — the worker downloads the bytes
    on its own and hands `bytes` to this function. We deserialise
    locally on the worker.
    """
    weights = deserialise_weights(weights_blob)         # bytes -> model state
    agent   = build_agent(weights)
    env     = build_pong_env(seed=seed)

    trajectories = []
    for _ in range(episodes_per_task):
        obs = env.reset()
        episode = []
        done = False
        while not done:
            action = agent.act(obs)
            next_obs, reward, done, _ = env.step(action)
            episode.append((obs, action, reward))
            obs = next_obs
        trajectories.append(episode)

    return {"trajectories": trajectories, "seed": seed}


def train(num_iterations: int = 1000, num_workers: int = 32, episodes_per_task: int = 8):
    weights = init_weights()
    optimizer = build_optimizer(weights)

    with PymonikClient() as client:
        with client.session(
            partition="pymonik",
            deps=["torch", "gymnasium[atari]", "ale-py"],
        ) as s:
            for it in range(num_iterations):
                # Upload current weights once; every rollout task
                # references the same blob_id.
                wblob = blob.upload(serialise_weights(weights))

                rollouts = collect_rollouts.starmap(
                    (wblob, episodes_per_task, it * num_workers + w)
                    for w in range(num_workers)
                )

                results = rollouts.results(timeout=600)
                trajectories = [t for r in results for t in r["trajectories"]]

                loss = update_weights(weights, optimizer, trajectories)
                if it % 10 == 0:
                    print(f"iter {it}: loss={loss:.4f}, episodes={len(trajectories)}")
```

## Why blobs matter here

The model weights are passed to every rollout task. With
`num_workers=32`, naïvely passing `weights_blob` inline would mean 32
cloudpickled copies of the weights per training iteration. With
`blob.upload(...)` it's one upload + 32 references. PymoniK's
auto-spill kicks in for any arg over 256 KiB, so even without the
explicit `blob.upload` you'd get this dedup behaviour — but doing it
explicitly is clearer in code and skips the cloudpickle round-trip on
every iteration.

## Why runtime deps matter here

Running PyTorch and Gymnasium on the worker doesn't require baking
those into the image — declare them on the session:

```python
client.session(
    partition="pymonik",
    deps=["torch", "gymnasium[atari]", "ale-py"],
)
```

The first task on a fresh worker pod pays the install (multi-minute
for torch); subsequent tasks reuse the venv. For production runs,
bake torch into the image instead — see
[Worker images](../guides/worker-images.md).

## Things to add

- **GPU partition.** Move rollouts to a GPU partition for faster
  inference: `client.session(partition=["cpu", "gpu"], deps=["torch"])`
  + `collect_rollouts.with_options(partition="gpu")`. See
  [Multi-partition routing](../guides/multi-partition.md).
- **Async streaming.** Use `as_completed(rollouts)` instead of
  `.results()` to start training on the first rollouts that arrive
  rather than waiting for the slowest. See
  [Async](../guides/async.md).
- **Retries.** Rollouts that crash on a flaky pod aren't fatal —
  `@task(retries=3)` on `collect_rollouts` recovers transparently.
  See [Retries](../guides/retries.md).
