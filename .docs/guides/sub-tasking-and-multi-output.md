# Sub-tasking and multi-output tasks

Two related primitives, both about controlling how a task's output flows
into the cluster:

- **`task.tail(*args)`** — sub-tasking. Lets a `@task` body delegate its
  output to another task. Replaces what other frameworks call
  "tail-call" or "delegation."
- **`MultiResult(field=value, ...)`** — multi-output tasks. A single task
  produces N independently-named outputs that downstream consumers
  depend on individually.

They compose: a multi-output task can tail-call to another multi-output
task, and a multi-output task can use `tail()` to delegate just one
field's computation to a child task.

## Sub-tasking with `task.tail()`

```python
from pymonik import task

@task
def base(n: int) -> int:
    return n + 1

@task
def adaptive(n: int) -> int:
    if n < 1024:
        return base.tail(n)        # delegate to base; base writes our output
    return n
```

When `adaptive(2)` is invoked remotely, the worker:

1. Runs the function. It returns `base.tail(2)` — a `TailPromise`.
2. Submits `base` as a child task whose ArmoniK
   `expected_output_ids` is set to *adaptive's* expected output id.
3. Returns. The cluster delivers `base`'s result to whoever was
   awaiting `adaptive`.

The user's submission code never sees the difference:

```python
with client.session(partition="pymonik") as s:
    print(adaptive.spawn(2).result(timeout=10))     # 3, via base
    print(adaptive.spawn(2000).result(timeout=10))  # 2000, no delegation
```

### `tail()` is lazy

`task.tail(*args)` does **not** submit a task immediately. It returns a
`TailPromise` that the framework will submit later, with whichever
output id is appropriate (the parent's output, or a specific
MultiResult field's output). Three rules:

- **Awaiting a `TailPromise` directly is an error.** It hasn't been
  submitted; there's nothing to await. If you want the result, use
  `task.spawn(...)` instead.
- **A `TailPromise` is only valid as a return value of a `@task`** —
  either returned directly, or as a field value inside a
  `MultiResult`. Anywhere else it's an error.
- **A worker that constructs a `TailPromise` and drops it on the
  floor** (returns something else without binding it) leaks: the
  child task is never submitted. There's no warning today; we may
  add one.

### `tail()` chains

A tail-called task can itself tail-call:

```python
@task
def increment_chain(n: int, acc: int) -> int:
    if n == 0:
        return acc
    return increment_chain.tail(n - 1, acc + 1)
```

Each link's child writes to the *original* parent's output id (since
the chain unwinds — every intermediate task's output id is the same).
ArmoniK handles arbitrary depth.

## Multi-output tasks with `MultiResult`

```python
from pymonik import MultiResult, task

@task
def split(x: int):
    return MultiResult(double=x * 2, triple=x * 3)
```

`split.spawn(7)` returns a `MultiResultHandle`, not a `Future`:

```python
with client.session(partition="pymonik") as s:
    out = split.spawn(7)
    out.double.result()        # 14 — only blocks on the `double` output
    out.triple.result()        # 21 — only blocks on the `triple` output
    view = out.result()        # MultiResultView — blocks on every field
    view.double                # 14    (attribute access)
    view["double"]             # 14    (dict-style access)
    dict(view)                 # {"double": 14, "triple": 21}
```

Each field is its own ArmoniK output id. A downstream task that
consumes one field doesn't wait on the other:

```python
@task
def double_plus_one(d: int) -> int:
    return d + 1

with client.session(partition="pymonik") as s:
    out = split.spawn(7)
    answer = double_plus_one.spawn(out.double)   # depends only on `double`
    answer.result()             # 15 — runs before `triple` finishes
```

That independent-scheduling behaviour is the reason to use
`MultiResult` rather than returning a dataclass: a slow `triple`
doesn't gate consumers of `double`.

### How the schema is extracted

The `@task` decorator walks the function body's AST at decoration
time, finds every `MultiResult(...)` literal, and validates that all
branches use the same field set:

```python
@task
def conditional(x: int):
    if x > 0:
        return MultiResult(a=x, b=-x)
    return MultiResult(a=-x, b=x)        # ← same field set; OK
```

Branches with inconsistent shapes raise at decoration:

```python
@task
def bad(x: int):
    if x > 0:
        return MultiResult(a=x, b=x)
    return MultiResult(a=x, b=x, c=x)    # ← raises PymonikError on import
```

The error has the offending lines. Bugs that would silently mis-write
outputs in production show up at module-load time.

### Limitations of AST extraction

- **Helpers don't count.** `MultiResult(...)` constructed in a helper
  function the task calls is invisible to the AST walk.
- **`**kwargs` expansion is rejected.** `MultiResult(**dynamic)` would
  produce a non-static field set; the decorator raises.
- **Aliased imports work.** `from pymonik import MultiResult as MR;
  return MR(a=..., b=...)` is fine — the walker tracks top-level
  imports.

If you need to construct `MultiResult` outside the task body, declare
the schema explicitly via the decorator:

```python
@task(outputs=("a", "b"))
def via_helper(x):
    return _build_outputs(x)

def _build_outputs(x):
    return MultiResult(a=x, b=-x)
```

`outputs=(...)` overrides the AST walk; the decorator trusts your
declared field set.

### `MultiResult` returning the wrong shape fails the task

Even with AST extraction in place, what actually flows at runtime is
checked again on the worker. A task that declared
`MultiResult(a=int, b=int)` but returns `MultiResult(a=int)` (perhaps
via a helper) fails:

```text
TaskFailed: MultiResult shape mismatch (missing ['b']).
Declared: ['a', 'b']; returned: ['a'].
```

## Per-field tail-call: `MultiResult(a=other.tail(...))`

A `MultiResult` field's value can be a plain Python value (cloudpickled
and written by the parent worker) **or** a `TailPromise` (delegated to
a child task that writes that one field's output):

```python
@task
def heavy_compute(x: int) -> int:
    # ... slow ...
    return x * 100

@task
def split(x: int):
    return MultiResult(
        cheap=x + 1,                    # written by split's worker
        expensive=heavy_compute.tail(x),# delegated; heavy_compute's worker writes it
    )
```

The cluster runs:

- `split`'s worker writes `cheap`'s bytes to its output id and submits
  `heavy_compute` as a child with the `expensive` output id.
- `heavy_compute` runs (possibly on a different partition / pod) and
  writes its result to `expensive`'s output id.
- A downstream consumer of `out.cheap` runs immediately; a consumer of
  `out.expensive` waits for `heavy_compute`.

### Rules for `MultiResult` fields

- **Plain values** — pickled, written directly. Use for fast-to-compute
  fields.
- **`TailPromise` from `task.tail(...)`** — delegated to a child task.
  Use when one field is expensive enough to warrant its own task.
- **`Future` from `task.spawn(...)`** — **error**. The Future has its
  own output id (allocated when `spawn` ran inside the parent worker);
  binding it to a MultiResult field would mean re-routing already-
  submitted work, which the cluster can't do cheaply. Use `tail()`
  instead.
- **Multi-output children** (`MultiResult(a=other_split.tail(x))`
  where `other_split` is itself multi-output) — **error**. Per-field
  delegation requires a single-output child. To wire up a nested
  multi-output result, insert a passthrough single-output task that
  forwards just the field you want.

## Whole-task tail-call to a multi-output child

A multi-output task can tail-call another multi-output task — the
shapes must match:

```python
@task
def rebranded(x: int):
    return MultiResult(a=x * 2, b=x * 3)

@task
def parent(x: int):
    if x > 100:
        return rebranded.tail(x)         # OK: child declares same shape
    return MultiResult(a=x, b=x * 5)
```

If the schemas differ, the worker raises clearly:

```text
worker error: tail-called task 'wrong_shape' declares ['x', 'y', 'z']
but parent declares ['a', 'b'] — shapes must match for whole-task tail-call.
```

## Cancellation

`MultiResultHandle.cancel()` cancels the task that produces all of the
handle's outputs. ArmoniK's `Tasks.CancelTasks` operates per-task —
there's no "cancel just one output of a task." Every field's Future
resolves to `TaskCancelled`.

For tail-call chains, cancelling the parent cancels the chain: ArmoniK
propagates cancellation to children when a parent is cancelled.

## Cluster behaviour matches local

Everything documented here works the same under `LocalCluster` for
testing — same envelope encoding, same dispatch logic. See the
[Local testing](local-testing.md) guide.

## When to reach for which

- **Want a single result?** Plain `@task` returning a single value.
- **Want a single result, decided dynamically by another task?**
  `task.tail(...)` returned from the body.
- **Want N results that downstream tasks consume independently?**
  `MultiResult(...)` with the field set extracted at decoration.
- **Want a structured result that downstream consumers always read
  whole?** Plain `@task` returning a dataclass — no need for
  `MultiResult`. One ArmoniK output, one data-dependency edge per
  consumer; less ceremony.
