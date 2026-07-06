"""Multiple-named-output tasks and lazy tail-call promises.

Two shapes a ``@task`` body can return:

- :class:`MultiResult` — a runtime container of named outputs. The
  ``@task`` decorator extracts the field set by walking the function's
  AST at decoration time, so the framework knows ahead of time how many
  ArmoniK ``expected_output_ids`` to allocate for each task. Downstream
  consumers depend on individual fields, not on the whole task — fast
  fields don't gate slow ones.

- :class:`TailPromise` — a lazy submission marker returned by
  :meth:`pymonik.Task.tail`. The framework decides which output id to
  bind it to (the parent's output, or a specific MultiResult field's
  output) and submits only when the parent ``@task`` returns.

Valid uses of a ``TailPromise``:

- Returned directly from a ``@task`` (whole-task tail-call).
- As a field value inside a returned ``MultiResult`` (per-field
  tail-call).

Anything else — passing a TailPromise to another ``.spawn()``, awaiting
one, storing one and dropping it on the floor — raises a clear error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pymonik.errors import PymonikError

if TYPE_CHECKING:
    from pymonik.task import Task

R = TypeVar("R")


class TailPromise(Generic[R]):
    """A lazy task submission. Bound to an output id by the framework."""

    __slots__ = ("_task", "_args", "_kwargs")

    def __init__(
        self,
        task: "Task[Any, R]",
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._task = task
        self._args = args
        self._kwargs = kwargs

    @property
    def task(self) -> "Task[Any, R]":
        return self._task

    def __await__(self):
        raise PymonikError(
            "TailPromise cannot be awaited. `task.tail(...)` is for use inside a "
            "@task body — return it (whole-task tail-call) or place it as a "
            "MultiResult field value (per-field tail-call). To submit and "
            "await, use `task.spawn(...)` instead."
        )

    def __repr__(self) -> str:
        return f"TailPromise({self._task.name})"


class MultiResult:
    """A bag of named outputs returned by a multi-output ``@task``.

    Construct in the function body::

        @task
        def split(x: int):
            return MultiResult(double=x * 2, triple=x * 3)

    Field values can be plain Python values (cloudpickled and written
    by the worker) or :class:`TailPromise` instances (submitted as
    delegated child tasks whose outputs land on the field's
    ``result_id``). A :class:`pymonik.Future` from ``.spawn()`` is
    rejected — use ``.tail()`` for delegation.

    The framework reads the field set from this constructor's keyword
    arguments via AST analysis at ``@task`` decoration time. Every
    ``MultiResult(...)`` literal in the function body must use the
    same field names; otherwise the decoration raises.
    """

    __slots__ = ("_fields",)

    # Names that ``MultiResultHandle`` exposes as properties or methods.
    # A field with one of these names would be shadowed by attribute
    # lookup on the handle (e.g. ``out.result()`` would call the handle
    # method, not return the field Future). Reject at construction.
    _RESERVED_FIELD_NAMES = frozenset(
        {"task_id", "fields", "done", "result", "cancel"}
    )

    def __init__(self, **fields: Any) -> None:
        if not fields:
            raise PymonikError(
                "MultiResult requires at least one field"
            )
        for name in fields:
            if name.startswith("_"):
                raise PymonikError(
                    f"MultiResult field name {name!r} is invalid: "
                    f"underscore-prefixed names are reserved."
                )
            if name in MultiResult._RESERVED_FIELD_NAMES:
                raise PymonikError(
                    f"MultiResult field name {name!r} collides with a "
                    f"MultiResultHandle attribute. Reserved names: "
                    f"{sorted(MultiResult._RESERVED_FIELD_NAMES)}."
                )
        # Avoid __setattr__ collision if we ever add @dataclass-like behaviour.
        object.__setattr__(self, "_fields", dict(fields))

    @property
    def fields(self) -> dict[str, Any]:
        """The field mapping. Values may be plain or ``TailPromise``s."""
        return self._fields

    def __repr__(self) -> str:
        parts = ", ".join(f"{k}={v!r}" for k, v in self._fields.items())
        return f"MultiResult({parts})"
