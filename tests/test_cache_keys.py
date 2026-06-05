"""Structural cache keys + result index.

These are pure-logic tests (no cluster): they prove the content-address
properties the reuse cache relies on — most importantly that an
unchanged upstream task keeps a stable key when a downstream task
changes, which is what lets a DAG-prefix be reused.
"""

from __future__ import annotations

from pymonik._internal.exec_cache import (
    ResultIndex,
    arg_descriptor,
    compute_cache_key,
    fn_identity,
)
from pymonik.future import Future

V = "2.0.0a"


def _future_with_key(key: str | None) -> Future:
    f: Future = Future.__new__(Future)
    f._cache_key = key
    return f


def _key(fn, args=(), kwargs=None, task_name="t", cache_version=None) -> str | None:
    return compute_cache_key(
        pymonik_version=V,
        task_name=task_name,
        fn_id=fn_identity(fn, cache_version=cache_version),
        args=args,
        kwargs=kwargs or {},
    )


# ---------- fn identity ----------


def test_same_function_same_identity():
    def f(x):
        return x + 1

    assert fn_identity(f) == fn_identity(f)


def test_body_change_changes_identity():
    def f1(x):
        return x + 1

    def f2(x):
        return x + 2

    assert fn_identity(f1) != fn_identity(f2)


def test_cache_version_override_is_stable_across_body():
    def f1(x):
        return x + 1

    def f2(x):
        return x + 999  # different body

    # Same declared version → same identity regardless of body.
    assert fn_identity(f1, cache_version="v1") == fn_identity(f2, cache_version="v1")
    assert fn_identity(f1, cache_version="v1") != fn_identity(f1, cache_version="v2")


def test_closure_value_participates():
    def make(n):
        def f(x):
            return x + n
        return f

    assert fn_identity(make(1)) != fn_identity(make(2))  # different closed-over n
    assert fn_identity(make(1)) == fn_identity(make(1))


# ---------- key composition ----------


def test_concrete_args_change_key():
    def f(x):
        return x

    assert _key(f, args=(1,)) != _key(f, args=(2,))
    assert _key(f, args=(1,)) == _key(f, args=(1,))


def test_kwargs_order_independent():
    def f(**kw):
        return kw

    assert _key(f, kwargs={"a": 1, "b": 2}) == _key(f, kwargs={"b": 2, "a": 1})


# ---------- the Merkle property (the headline) ----------


def test_future_arg_contributes_upstream_key():
    def consume(v):
        return v

    up_a = _future_with_key("KEY_A")
    up_b = _future_with_key("KEY_B")
    # Same consumer, different upstream identities → different keys.
    assert _key(consume, args=(up_a,)) != _key(consume, args=(up_b,))
    # Same upstream identity → same key (reuse).
    assert _key(consume, args=(up_a,)) == _key(consume, args=(_future_with_key("KEY_A"),))


def test_unchanged_upstream_key_is_independent_of_downstream():
    # The scenario: A -> B -> C. Change C; A and B must keep stable keys
    # so their already-computed results are reused. A and B keys are
    # computed from their own fn+inputs and never reference downstream —
    # so they're trivially independent. Verify a downstream change does
    # not perturb the upstream key.
    def A(x):
        return x

    key_A_run1 = _key(A, args=(5,))
    # ... C changes between runs, but A's key only depends on A + its arg:
    key_A_run2 = _key(A, args=(5,))
    assert key_A_run1 == key_A_run2

    # And B (consuming A) stays stable iff A's key stays stable:
    def B(v):
        return v + 1

    upA = _future_with_key(key_A_run1)
    assert _key(B, args=(upA,)) == _key(B, args=(_future_with_key(key_A_run2),))


def test_uncacheable_when_upstream_uncacheable():
    def consume(v):
        return v

    # An upstream future with no cache key (uncacheable upstream) makes
    # the consumer uncacheable too.
    assert _key(consume, args=(_future_with_key(None),)) is None


def test_uncacheable_when_arg_unpicklable():
    def consume(v):
        return v

    assert arg_descriptor(lambda: 0) is not None  # lambdas pickle via cloudpickle
    # an unpicklable leaf:
    import threading

    assert arg_descriptor(threading.Lock()) is None
    assert _key(consume, args=(threading.Lock(),)) is None


# ---------- result index ----------


def test_result_index_roundtrip(tmp_path):
    idx = ResultIndex(tmp_path)
    assert idx.get("abc123") is None
    idx.put("abc123", result_id="rid-1", session_id="sess-1")
    got = idx.get("abc123")
    assert got == {"result_id": "rid-1", "session_id": "sess-1"}

    # Persists for a fresh instance (cross-run reuse).
    idx2 = ResultIndex(tmp_path)
    assert idx2.get("abc123")["result_id"] == "rid-1"

    idx.forget("abc123")
    assert idx.get("abc123") is None
