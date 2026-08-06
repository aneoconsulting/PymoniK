# Metropolis-Hastings Sampling

This example draws samples from a 2D target distribution using the
[Metropolis-Hastings](https://en.wikipedia.org/wiki/Metropolis%E2%80%93Hastings_algorithm)
algorithm, and distributes independent chains ("trials") across an ArmoniK
cluster with PymoniK.

The target distribution is:

```
p(x, y) = exp(-10 * (x^2 - y)^2 - (y - 0.25)^4)
```

Each trial runs a Markov chain of `sizeSample` steps starting from the same
initial point `(1.5, -0.8)`, proposing moves with a Gaussian random walk of
standard deviation `gamma`, and accepting or rejecting them according to the
Metropolis-Hastings acceptance ratio. A `burn_in` fraction of each chain's
samples is dropped before the chain has converged to the target distribution.

Because the trials don't depend on each other, `run_metropolis_trial` is a
PymoniK `@task` and every trial is submitted as a separate task via
`map_invoke`, so all chains sample in parallel instead of running one after
another.

## Requirements

This example needs a running ArmoniK cluster. See the top-level
[README](../../README.md) and the
[ArmoniK getting started guide](https://armonik.readthedocs.io/en/latest/content/armonik/getting-started.html)
for deployment instructions.

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Running

The script connects to `localhost:5001` by default; edit the `endpoint`
argument passed to `Pymonik(...)` in `metropolis.py` if your cluster is
reachable elsewhere (or export `AKCONFIG` pointing at a config file and
drop the `endpoint` argument instead).

```bash
uv run metropolis.py <trials> <sizeSample> <gamma> [--burn_in BURN_IN]
```

- `trials`: number of independent chains to run (one PymoniK task each).
- `sizeSample`: number of samples per chain.
- `gamma`: standard deviation of the Gaussian proposal step.
- `--burn_in`: fraction of each chain to discard as burn-in (default: `0.1`).

For example:

```bash
uv run metropolis.py 100 1000 0.05
```

## Output

The script writes one `metrop<i>.vtk` file per trial (post-burn-in sample
points as circle glyphs) plus `target_distribution.vtk` (the target density
over a `200x200` grid). Open them together in [ParaView](https://www.paraview.org/)
to overlay the sampled points on the target distribution's contour.
