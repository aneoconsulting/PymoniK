"""Hashing rules for ``compute_env_id``.

Two clients submitting the *same* deps must land in the *same* venv.
That contract is what makes /cache/internal sharing work, so the
canonicalisation rules are tested explicitly here.
"""

from __future__ import annotations

from pymonik._internal.env_builder import canonical_deps, compute_env_id
from pymonik.envelope import EnvSpec


def test_canonical_deps_strip_lower_dedup_sort():
    assert canonical_deps(["  numpy  ", "POLARS", "numpy", ""]) == ("numpy", "polars")


def test_env_id_is_order_independent():
    a = EnvSpec(deps=("numpy", "polars", "scikit-learn==1.5.*"))
    b = EnvSpec(deps=("scikit-learn==1.5.*", "polars", "numpy"))
    assert compute_env_id(a) == compute_env_id(b)


def test_env_id_is_case_independent_for_names():
    a = EnvSpec(deps=("NumPy",))
    b = EnvSpec(deps=("numpy",))
    assert compute_env_id(a) == compute_env_id(b)


def test_env_id_changes_with_specifier():
    a = EnvSpec(deps=("numpy>=2",))
    b = EnvSpec(deps=("numpy",))
    assert compute_env_id(a) != compute_env_id(b)


def test_env_id_changes_with_index_url():
    base = EnvSpec(deps=("numpy",))
    other = EnvSpec(deps=("numpy",), index_url="https://my.private.index/")
    assert compute_env_id(base) != compute_env_id(other)


def test_env_id_independent_of_isolate_flag():
    """``isolate`` is a dispatch-mode toggle, not part of the env identity:
    a session that splices and a session that subprocesses should reuse
    the same venv on disk."""
    a = EnvSpec(deps=("numpy",), isolate=True)
    b = EnvSpec(deps=("numpy",), isolate=False)
    assert compute_env_id(a) == compute_env_id(b)


def test_env_id_short_and_hexlike():
    spec = EnvSpec(deps=("numpy",))
    h = compute_env_id(spec)
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)
