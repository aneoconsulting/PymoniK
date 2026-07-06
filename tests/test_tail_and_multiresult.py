"""Tail-call sub-tasking + multi-output tasks.

Two related primitives:

- ``task.tail(*args)`` returns a ``TailPromise``; the framework binds it
  to an output id at worker dispatch time. Replaces the old
  ``_delegate=True`` flag.
- ``MultiResult(field=...)`` runs a multi-output task; the field set is
  extracted from the function body's AST at decoration time. Each field
  becomes an independent ArmoniK output id; downstream tasks block only
  on the field they consume.

These tests use ``LocalCluster`` end-to-end (real envelope encoding,
real ref resolution, real dispatcher logic). Cluster behaviour mirrors
the local path bit-for-bit.
"""

from __future__ import annotations

import pytest

from pymonik import (
    MultiResult,
    MultiResultHandle,
    PymonikConnectionError,
    TailPromise,
    TaskFailed,
    task,
)
from pymonik.errors import PymonikError
from pymonik.multiresult import MultiResult as _MR  # alias for AST tests
from pymonik.testing import LocalCluster


# ---------- decoration-time AST extraction ----------


def test_extract_simple_multiresult_shape():
    @task
    def split(x: int):
        return MultiResult(double=x * 2, triple=x * 3)

    assert split.multi_fields == ("double", "triple")


def test_extract_consistent_branches():
    @task
    def conditional(x: int):
        if x > 0:
            return MultiResult(a=x, b=-x)
        return MultiResult(a=-x, b=x)

    assert conditional.multi_fields == ("a", "b")


def test_inconsistent_branches_raises_at_decoration():
    with pytest.raises(PymonikError, match="inconsistent MultiResult shapes"):

        @task
        def bad(x: int):
            if x > 0:
                return MultiResult(a=x, b=x)
            return MultiResult(a=x, b=x, c=x)


def test_kwargs_expansion_is_rejected():
    with pytest.raises(PymonikError, match="\\*\\*kwargs"):

        @task
        def bad(x):
            d = {"a": 1, "b": 2}
            return MultiResult(**d)


def test_no_multiresult_means_single_output_task():
    @task
    def regular(x: int) -> int:
        return x * 2

    assert regular.multi_fields is None


def test_explicit_outputs_decorator_kwarg():
    @task(outputs=("alpha", "beta"))
    def via_helper(x):
        # AST can't see the construction; explicit outputs declares it.
        return _build_mr(x)

    def _build_mr(x):
        return MultiResult(alpha=x, beta=-x)

    assert via_helper.multi_fields == ("alpha", "beta")


# ---------- tail-call basics ----------


def test_tail_returns_tailpromise():
    @task
    def add(a, b):
        return a + b

    promise = add.tail(2, 3)
    assert isinstance(promise, TailPromise)
    assert promise.task is add


def test_tail_promise_cant_be_awaited_directly():
    @task
    def add(a, b):
        return a + b

    promise = add.tail(2, 3)

    async def _try():
        await promise

    import asyncio

    with pytest.raises(PymonikError, match="cannot be awaited"):
        asyncio.run(_try())


def test_old_delegate_kwarg_raises():
    @task
    def add(a, b):
        return a + b

    with LocalCluster() as client:
        with client.session() as s:
            with pytest.raises(PymonikError, match="_delegate=True"):
                add.spawn(2, 3, _delegate=True)


# ---------- whole-task tail-call (single-output) ----------


def test_whole_task_tail_call_end_to_end():
    @task
    def grow(n: int) -> int:
        return n + 1

    @task
    def adaptive(n: int) -> int:
        if n < 5:
            return grow.tail(n)
        return n

    with LocalCluster() as client:
        with client.session() as s:
            assert adaptive.spawn(2).result(timeout=15) == 3
            assert adaptive.spawn(7).result(timeout=15) == 7


def test_tail_call_chain():
    """A tail-called task can itself tail-call. Each leaf writes to the
    chain's original parent output id."""

    @task
    def increment_chain(n: int, acc: int) -> int:
        if n == 0:
            return acc
        return increment_chain.tail(n - 1, acc + 1)

    with LocalCluster() as client:
        with client.session() as s:
            assert increment_chain.spawn(5, 0).result(timeout=20) == 5


# ---------- multi-output tasks ----------


def test_spawn_returns_multiresulthandle_for_multi_output_task():
    @task
    def split(x: int):
        return MultiResult(double=x * 2, triple=x * 3)

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(7)
            assert isinstance(out, MultiResultHandle)
            assert set(out.fields) == {"double", "triple"}


def test_per_field_access_is_independent_future():
    @task
    def split(x: int):
        return MultiResult(double=x * 2, triple=x * 3)

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(5)
            assert out.double.result(timeout=10) == 10
            assert out.triple.result(timeout=10) == 15


def test_handle_result_returns_view_supporting_attr_and_dict_access():
    @task
    def split(x: int):
        return MultiResult(double=x * 2, triple=x * 3)

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(4)
            resolved = out.result(timeout=10)
            # Equality with plain dict — backwards-compat with prior return type.
            assert resolved == {"double": 8, "triple": 12}
            # Attribute access.
            assert resolved.double == 8
            assert resolved.triple == 12
            # Dict-style access.
            assert resolved["double"] == 8
            assert resolved["triple"] == 12
            # Iter, len, contains, dict()-coercion all work.
            assert sorted(resolved) == ["double", "triple"]
            assert len(resolved) == 2
            assert "double" in resolved
            assert dict(resolved) == {"double": 8, "triple": 12}
            # Repr is field=value form, no quoted keys.
            assert "double=8" in repr(resolved)
            assert "triple=12" in repr(resolved)


def test_handle_result_view_unknown_attr_raises_attribute_error():
    @task
    def split(x: int):
        return MultiResult(a=x, b=x * 2)

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(3)
            view = out.result(timeout=10)
            with pytest.raises(AttributeError, match="not a field"):
                _ = view.nonexistent


def test_field_can_feed_downstream_task():
    """Independent scheduling: a downstream task that consumes one field
    runs as soon as that field arrives."""

    @task
    def split(x: int):
        return MultiResult(a=x * 2, b=x * 3)

    @task
    def add_one(v: int) -> int:
        return v + 1

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(10)
            d = add_one.spawn(out.a)
            assert d.result(timeout=10) == 21


def test_returning_wrong_shape_fails_task():
    @task
    def lying(x: int):
        # AST would catch this if the literal differed; but we make
        # the runtime see a different *value* shape via a manual
        # MultiResult construction. Decoration extracts {"a", "b"};
        # at runtime we omit "b".
        return MultiResult(a=x)

    # AST extracted the lying call's fields literally — {"a"}. So
    # technically this wouldn't raise. Use the explicit-outputs path
    # to force a mismatch.

    @task(outputs=("a", "b"))
    def really_lying(x: int):
        return MultiResult(a=x, c=x)  # ← AST sees {a, c}, but explicit outputs says {a, b}

    # The explicit outputs win. At runtime the worker sees {a, c}
    # vs declared {a, b} — should fail with shape mismatch.
    with LocalCluster() as client:
        with client.session() as s:
            fut = really_lying.spawn(7).a
            with pytest.raises(TaskFailed, match="shape mismatch"):
                fut.result(timeout=10)


def test_returning_plain_value_from_multi_output_task_fails():
    @task(outputs=("a", "b"))
    def pretender(x):
        return x * 2  # plain int instead of MultiResult

    with LocalCluster() as client:
        with client.session() as s:
            fut = pretender.spawn(5).a
            with pytest.raises(TaskFailed, match="MultiResult"):
                fut.result(timeout=10)


def test_returning_multiresult_from_single_output_task_fails():
    @task
    def pretender(x):
        # Build MultiResult dynamically so AST doesn't see it
        cls = MultiResult
        return cls(a=x, b=x)

    with LocalCluster() as client:
        with client.session() as s:
            with pytest.raises(TaskFailed, match="MultiResult"):
                pretender.spawn(7).result(timeout=10)


# ---------- per-field tail-call ----------


def test_per_field_tail():
    @task
    def heavy(x: int) -> int:
        return x * 100

    @task
    def split(x: int):
        return MultiResult(
            heavy_double=heavy.tail(x * 2),
            quick=x + 1,
        )

    with LocalCluster() as client:
        with client.session() as s:
            out = split.spawn(5)
            assert out.heavy_double.result(timeout=15) == 1000
            assert out.quick.result(timeout=15) == 6


def test_per_field_tail_to_multi_output_task_rejected():
    @task
    def inner_split(x: int):
        return MultiResult(p=x, q=x)

    @task
    def outer(x: int):
        return MultiResult(
            a=inner_split.tail(x),  # multi-output child — not allowed per-field
            b=x,
        )

    with LocalCluster() as client:
        with client.session() as s:
            with pytest.raises(TaskFailed, match="multi-output"):
                outer.spawn(5).a.result(timeout=10)


def test_per_field_future_from_spawn_is_rejected():
    """Inside a MultiResult, fields should not be Futures from .spawn().
    Use .tail() instead."""

    @task
    def inner(x: int) -> int:
        return x * 2

    @task
    def parent(x: int):
        # Calling .spawn() inside the worker creates a worker-stub
        # Future; placing it as a MultiResult field is rejected.
        return MultiResult(a=inner.spawn(x), b=x + 1)

    with LocalCluster() as client:
        with client.session() as s:
            with pytest.raises(TaskFailed, match="\\.tail\\(\\) for delegation"):
                parent.spawn(7).a.result(timeout=10)


# ---------- whole-task tail-call across multi-output schemas ----------


def test_whole_task_tail_to_matching_multi_output_child():
    @task
    def child(x: int):
        return MultiResult(a=x * 2, b=x * 3)

    @task
    def parent(x: int):
        if x > 100:
            return child.tail(x)  # same shape as parent
        return MultiResult(a=x, b=x * 5)

    with LocalCluster() as client:
        with client.session() as s:
            small = parent.spawn(7).result(timeout=10)
            assert small == {"a": 7, "b": 35}
            big = parent.spawn(200).result(timeout=10)
            assert big == {"a": 400, "b": 600}


def test_whole_task_tail_with_mismatched_child_fails():
    @task
    def wrong_shape(x: int):
        return MultiResult(x=x, y=x, z=x)  # different fields

    @task
    def parent(x: int):
        return wrong_shape.tail(x)  # parent is single-output; child is multi

    with LocalCluster() as client:
        with client.session() as s:
            with pytest.raises(TaskFailed, match="multi-output"):
                parent.spawn(5).result(timeout=10)


# ---------- TailPromise repr / API ----------


def test_tail_promise_repr_has_task_name():
    @task
    def add(a, b):
        return a + b

    p = add.tail(1, 2)
    assert "add" in repr(p)


def test_multiresult_repr_has_fields():
    mr = MultiResult(a=1, b=2)
    assert "a=1" in repr(mr) and "b=2" in repr(mr)


def test_multiresult_empty_construction_rejected():
    with pytest.raises(PymonikError, match="at least one field"):
        MultiResult()


def test_multiresult_reserved_field_name_rejected():
    """Field names that collide with MultiResultHandle attributes raise."""
    with pytest.raises(PymonikError, match="collides with a MultiResultHandle"):
        MultiResult(task_id="x", b=1)
    with pytest.raises(PymonikError, match="collides with a MultiResultHandle"):
        MultiResult(result="x", b=1)


def test_multiresult_underscore_field_rejected():
    with pytest.raises(PymonikError, match="underscore-prefixed names are reserved"):
        MultiResult(_internal=1, b=2)


def test_decoration_rejects_reserved_field_in_outputs():
    with pytest.raises(PymonikError, match="collide with MultiResultHandle"):

        @task(outputs=("task_id", "value"))
        def bad(x):
            return MultiResult(task_id=x, value=x)


def test_decoration_rejects_cache_with_multi_output():
    """cache=True is incompatible with multi-output (no per-field cache)."""
    with pytest.raises(PymonikError, match="cache=True is not compatible"):

        @task(cache=True)
        def cached_split(x):
            return MultiResult(a=x, b=x * 2)


# ---------- H2: multi-output errors fail ALL fields, not just the first ----------
#
# The error paths used to resolve only the primary (first-field) future,
# leaving sibling fields hanging until session close — so awaiting the handle
# or any non-first field blocked. The old tests only ever read `.a`, so they
# passed while the bug was live. These read a non-first field and the handle.


def test_multi_output_validation_error_fails_all_fields():
    @task(outputs=("a", "b"))
    def returns_plain(x):
        return x  # plain int, not MultiResult → declared-multi error

    with LocalCluster() as client:
        with client.session():
            handle = returns_plain.spawn(5)
            # Non-first field must fail promptly, not hang to a timeout.
            with pytest.raises(TaskFailed):
                handle.b.result(timeout=15)
            # The whole handle (blocks on every field) must fail too.
            with pytest.raises(TaskFailed):
                handle.result(timeout=15)


def test_multi_output_runtime_exception_fails_all_fields():
    # The common case beyond validation: the task body just raises.
    @task(outputs=("a", "b"))
    def boom(x):
        raise ValueError("kaboom")

    with LocalCluster() as client:
        with client.session():
            handle = boom.spawn(5)
            with pytest.raises(TaskFailed):
                handle.b.result(timeout=15)
            with pytest.raises(TaskFailed):
                handle.a.result(timeout=15)


# ---------- multi-output tasks × batch doors ----------
# .map() of a multi-output task yields a FutureList of MultiResultHandles;
# every FutureList door and composition helper must accept that mix.
# results()/outcomes() used to crash with AttributeError (_result/_settle
# only existed on Future).


@task
def _split(x: int):
    return MultiResult(double=x * 2, triple=x * 3)


def test_multi_output_map_results():
    with LocalCluster() as client:
        with client.session():
            fl = _split.map(range(3))
            views = fl.results(timeout=15)
            assert [dict(v) for v in views] == [
                {"double": 0, "triple": 0},
                {"double": 2, "triple": 3},
                {"double": 4, "triple": 6},
            ]


def test_multi_output_map_outcomes():
    with LocalCluster() as client:
        with client.session():
            fl = _split.map(range(2))
            ocs = fl.outcomes(timeout=15)
            assert all(oc.ok for oc in ocs)
            assert [dict(oc.value) for oc in ocs] == [
                {"double": 0, "triple": 0},
                {"double": 2, "triple": 3},
            ]


def test_multi_output_map_await():
    import asyncio

    async def _run():
        async with LocalCluster() as client:
            async with client.session_async():
                views = await _split.map(range(2))
                assert [dict(v) for v in views] == [
                    {"double": 0, "triple": 0},
                    {"double": 2, "triple": 3},
                ]

    asyncio.run(_run())


def test_multi_output_as_completed_and_gather_fan_out_per_field():
    from pymonik import as_completed, gather

    with LocalCluster() as client:
        with client.session():
            fl = _split.map(range(2))
            # Composition operates per-field: 2 tasks × 2 fields = 4 futures.
            got = sorted(f.result(timeout=15) for f in as_completed(fl, timeout=15))
            assert got == [0, 0, 2, 3]
            assert sorted(gather(fl).results(timeout=15)) == [0, 0, 2, 3]


def test_multi_output_cache_decoration_time_rejection():
    """@task(cache=True) on a multi-output task fails fast at decoration."""
    with pytest.raises(PymonikError, match="not compatible with"):

        @task(cache=True)
        def split_cached(x: int):
            return MultiResult(a=x, b=x + 1)


def test_multi_output_cache_via_with_options_does_not_crash(tmp_path):
    """cache=True reaching a multi-output task past the decoration check
    (with_options / session default_options) is a no-op, not a crash.

    The reuse index maps one key to one result_id; tagging the handle with
    _cache_key used to raise AttributeError (no such slot).
    """
    with LocalCluster(cache=tmp_path) as client:
        with client.session():
            cached = _split.with_options(cache=True)
            first = dict(cached.spawn(3).result(timeout=15))
            again = dict(cached.spawn(3).result(timeout=15))
            assert first == again == {"double": 6, "triple": 9}


__all__ = [
    "PymonikConnectionError",
]  # silence unused-import warnings
