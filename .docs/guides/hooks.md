# Lifecycle hooks

PymoniK calls your code when something happens client-side: a session
opens, tasks are submitted, a future resolves, fails, or retries.
Register a callback with `pymonik.hooks` and you get a typed event
object each time.

It's a public, supported extension point — the same surface the Marimo
integration uses to drive its live views. Cost when nothing is
registered is essentially nil: the emit site reads one reference, sees
there are no subscribers, and returns without building an event.

## Quick start

```python
from pymonik import hooks, task

@hooks.on(hooks.TaskFailed)
def alert(ev: hooks.TaskFailed) -> None:
    print(f"{ev.task_id} failed: {ev.error_type}: {ev.message}")

@task
def add(a, b):
    return a + b

# any failure now prints, wherever it happens
```

`subscribe` takes every event; `on(EventType, ...)` filters by type and
also works as a decorator:

```python
unsub = hooks.subscribe(lambda ev: print(type(ev).__name__))
hooks.on(hooks.TaskCompleted, my_handler)   # call form → returns a disposer
unsub()                                       # idempotent unregister
```

## Events

All events subclass `PymonikEvent`, which carries `session_id` and a
`time.monotonic()` timestamp `at`. There are no duration fields — to
time a task, diff the `at` of its `TaskSubmitted` and `TaskCompleted`:

| Event | Fields (beyond `session_id`, `at`) |
|---|---|
| `SessionOpened` | `partitions`, `attached` |
| `SessionClosed` | `cancelled` |
| `TaskSubmitted` | `task_id`, `task_name`, `result_ids`, `data_dependencies`, `partition`, `attempt`, `created_by` |
| `TaskCompleted` | `task_id`, `result_id` |
| `TaskFailed` | `task_id`, `result_id`, `error_type`, `message` |
| `TaskRetried` | `task_id`, `attempt` |

`created_by` on `TaskSubmitted` is the parent task id when the
submission came from inside a `@task` body (a subtask); `None` for
ordinary client submissions.

## The contract (read before writing a hook)

- **Synchronous, on the publishing thread.** A hook runs on whatever
  thread reached the lifecycle point — the events-stream thread, a
  worker thread, the submitting thread. **Do the minimum and return.** A
  hook that blocks (does I/O, waits on a lock) stalls task resolution
  for *every* task. Offload real work to your own queue/thread:

  ```python
  import queue
  _q: queue.Queue = queue.Queue()
  hooks.subscribe(_q.put_nowait)        # cheap; a worker thread drains _q
  ```

- **Exceptions are isolated.** A hook that raises is caught, logged at
  `debug`, and the next hook still runs — your bug can't fail a task.

- **Live stream, not a log.** Fire-and-forget; no buffering or replay. A
  hook registered *after* an event fired does not see it. If you need
  history, seed from the introspection API (`session.tasks`) and use
  hooks for what happens next.

- **Client-side only.** Tasks a *worker* spawns (`.starmap` / `.tail()`
  from inside a `@task` on a real cluster) emit on the worker's process,
  not yours. Observe those via `session.tasks`. (Under `LocalCluster`,
  everything is in-process, so you see subtasks here too — that's what
  `created_by` is for.)

## What it isn't

- Not OpenTelemetry. OTel (`pymonik[otel]`) exports spans to a collector
  for distributed tracing; hooks are in-process typed callbacks. Use
  both if you like — see [Observability](observability.md).
- Not structured logging. structlog emits string-keyed lines for log
  sinks; hooks hand you a typed object to react to programmatically.

## Example: a tiny progress counter

```python
from pymonik import hooks
import threading

class Progress:
    def __init__(self):
        self.submitted = self.done = 0
        self._lock = threading.Lock()
        hooks.on(hooks.TaskSubmitted, self._sub)
        hooks.on(hooks.TaskCompleted, self._fin)

    def _sub(self, ev):
        with self._lock:
            self.submitted += 1

    def _fin(self, ev):
        with self._lock:
            self.done += 1
            print(f"{self.done}/{self.submitted}", end="\r")
```
