"""Array (membership) and struct (sub-field) filter grammar.

Array fields use ``field__contains=`` / ``field__notcontains=``; struct
fields use ``struct__subfield[__op]=`` and, for the task-options map,
``options__<key>=``. Build-level (hermetic) — asserts the right filter
shape is produced; the e2e suite checks the cluster accepts them.
"""

from __future__ import annotations

import pytest

from pymonik._internal.query import (
    PartitionQuery, ResultQuery, SessionQuery, TaskQuery, _QueryContext,
)


def _q(cls):
    ctx = _QueryContext(
        tasks=None, sessions=None, results=None, partitions=None,
        scoped_session_id=None,
    )
    return cls(ctx)


def _last(query):
    return query._state.filters[-1]


# ---------- arrays ----------

def test_array_contains_builds_array_filter():
    f = _last(_q(SessionQuery).where(partition_ids__contains="cpu"))
    assert type(f).__name__ == "ArrayFilter"
    assert "cpu" in str(f)


def test_array_notcontains_builds_array_filter():
    f = _last(_q(PartitionQuery).where(parent_partition_ids__notcontains="p"))
    assert type(f).__name__ == "ArrayFilter"


def test_array_field_rejects_scalar_equality():
    with pytest.raises(ValueError, match="only membership"):
        _q(SessionQuery).where(partition_ids="cpu")


# ---------- string contains regression ----------

def test_string_contains_now_builds():
    # Regression: previously called the operator *constant* -> TypeError.
    f = _last(_q(ResultQuery).where(name__contains="out"))
    assert type(f).__name__ == "StringFilter"
    f2 = _last(_q(ResultQuery).where(name__notcontains="tmp"))
    assert type(f2).__name__ == "StringFilter"


# ---------- structs ----------

def test_struct_typed_subfield():
    f = _last(_q(TaskQuery).where(options__partition_id="gpu"))
    assert type(f).__name__ == "StringFilter"
    n = _last(_q(TaskQuery).where(options__max_retries__gt=3))
    assert type(n).__name__ == "NumberFilter"


def test_struct_subfield_cross_resource():
    f = _last(_q(SessionQuery).where(options__partition_id="cpu"))
    assert type(f).__name__ == "StringFilter"


def test_struct_user_option_map_key():
    # Unknown sub-field -> treated as a user-defined option key.
    f = _last(_q(TaskQuery).where(options__my_user_key="v"))
    assert type(f).__name__ == "StringFilter"
    # ...with a trailing op suffix on a map key.
    f2 = _last(_q(TaskQuery).where(options__my_key__startswith="v"))
    assert type(f2).__name__ == "StringFilter"


def test_struct_output_error():
    f = _last(_q(TaskQuery).where(output__error__contains="boom"))
    assert type(f).__name__ == "StringFilter"


def test_struct_requires_subfield():
    with pytest.raises(ValueError, match="struct field"):
        _q(TaskQuery).where(options="x")


def test_struct_unknown_subfield_without_map():
    # OutputFilter has no __getitem__, so an unknown sub-field is rejected.
    with pytest.raises(ValueError, match="no sub-field"):
        _q(TaskQuery).where(output__bogus="x")
