"""TaskOpts deps/isolate/index_url merge semantics."""

from __future__ import annotations

from pymonik.options import EMPTY, TaskOpts


def test_deps_passthrough_on_merge():
    a = TaskOpts(deps=("numpy",))
    b = TaskOpts(retries=3)
    merged = a.merge(b)
    assert merged.deps == ("numpy",)
    assert merged.retries == 3


def test_deps_override_on_merge():
    a = TaskOpts(deps=("numpy",))
    b = TaskOpts(deps=("polars",))
    # Right-hand wins for non-None deps. Composition is intentional —
    # @task(deps=...) overrides session deps for that one task.
    assert a.merge(b).deps == ("polars",)


def test_isolate_default_inherits():
    a = TaskOpts(deps=("numpy",))
    assert a.isolate is None  # inherits → worker reads env_spec.isolate=True default


def test_isolate_explicit_false_propagates():
    a = TaskOpts(deps=("numpy",), isolate=False)
    b = EMPTY
    assert a.merge(b).isolate is False
    assert b.merge(a).isolate is False


def test_index_url_carries():
    a = TaskOpts(deps=("numpy",), index_url="https://idx.example/simple/")
    assert a.merge(EMPTY).index_url == "https://idx.example/simple/"
