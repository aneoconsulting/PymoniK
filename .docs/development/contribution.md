# Contributing

Thank you for considering a contribution. PymoniK is a small,
opinionated SDK and we'd like to keep it that way — but there's
plenty to do, and outside perspectives are welcome.

For repo-wide conventions, see ANEO's
[contribution guidelines on ArmoniK.CLI](https://github.com/aneoconsulting/ArmoniK.CLI/blob/main/CONTRIBUTING.md);
PymoniK follows the same shape.

## Before you start

- For non-trivial changes, open an issue to discuss the approach
  before writing code. Saves rounds.

## What we'd particularly like help with

Roughly in priority order — none of these are claimed; happy to
talk through any of them.

### Production-readiness

- **`pymonik image build` CLI.** Read the user's `pyproject.toml`,
  render a Dockerfile from a template, run `docker build`, print the
  tag. The most-asked-for missing piece.
- **OIDC / bearer-token credentials.** The `Credentials` class only
  handles mTLS. Ship a `BearerCredentials(token_provider=...)` that
  plugs into the gRPC channel via the metadata callback.
- **`pymonik doctor` CLI.** Hits the cluster's `Versions` and
  `Health` services, reports cluster compatibility with the local
  pymonik version, surfaces obvious misconfigs (events stream
  reachable, partition exists, AKCONFIG sane).

### Observability

- **Wire OTel into ArmoniK upstream** so cluster-side spans (polling
  agent, control plane, agent sidecar) chain into PymoniK's. The W3C
  trace context already propagates; the cluster just needs to emit
  spans under it. This is an upstream contribution, not a PymoniK PR.
- **Notebook display hooks.** `Future.__repr__` rendering a progress
  bar in Jupyter; `FutureList` showing a per-task heatmap.

### Performance

- **Cross-session blob reuse.** Use ArmoniK's `Results.import_data` to
  bind a fresh result id to data already uploaded in a prior session,
  driven by a local `~/.cache/pymonik/blobs/` hash-to-opaque-id index.
- **Per-session warm subprocess** for `deps=` + `isolate=True`. Spawn
  one child Python at session-open time, feed tasks through a Unix
  socket. Drops per-task startup from ~500 ms to ~1 ms while
  preserving subprocess isolation.

### Async core

- Drop the threading completion loop, port the events stream to
  `grpc.aio`, unify `Future` on a single `anyio.Event`. The threading
  bridge in `Future` is the largest piece of accidental complexity in
  the codebase.

### Tests and examples

- More end-to-end tests against a `testcontainers`-spun ArmoniK.
- `hypothesis` round-trip tests for the envelope and refs.

### Documentation

- This doc tree is a fresh rewrite; it'll have rough edges. Reading
  through any of the guides and filing an issue (or PR draft) for
  things that confused you is genuinely valuable.
- Worked examples for fault tolerance — show what happens when a
  worker pod gets evicted mid-task, and how `retries=` covers it.

## Small but appreciated

- Typo fixes, dead-link fixes, doctest fixes.
- Ruff / pyright cleanups in `_internal/`.
- More attribute coverage on existing OTel spans (anything that'd
  help filter in a UI).

## What we generally don't want

- **Major API churn** — the public surface (decorator, `.spawn`,
  `.map`, `Future`, `Blob`, `Materialize`) is mostly settled.
  Suggest naming changes via an issue first; don't rename in a PR.
- **Adding heavy dependencies** to the runtime. The current set is
  deliberate. New deps need a strong "it would be much worse to
  hand-roll this" argument.
- **Hiding ArmoniK from users.** PymoniK wraps the lower-level
  `armonik` package; it doesn't try to replace it. Anywhere you'd
  reach for `armonik.client.*`, that should still work alongside the
  PymoniK API.

## How to ship a PR (when you have permissions)

The maintainer's workflow:

1. Local commits, no force pushes to shared branches.
2. Run the fast suite: `uv run pytest -m "not slow"`.
3. Run pyright: `uv run basedpyright src/pymonik`.
4. Run ruff: `uv run ruff check && uv run ruff format`.
5. If you touched `worker.py` or anything in `_internal/`, rebuild
   the worker image and restart the partition; rerun a
   representative example end-to-end against the rebuilt cluster.
6. Open the PR with a clear description.
