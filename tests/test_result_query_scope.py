"""``session.results`` scopes via a server-side ``session_id`` filter.

ArmoniK's ``Result`` model exposes ``session_id`` as a filterable field,
so session-scoped result queries AND ``session_id == sid`` into the
``list_results`` call — the same shape :class:`TaskQuery` uses — instead
of enumerating the session's tasks and collecting their
``expected_output_ids``. These tests pin that wiring (and that no task
walk happens).
"""

from __future__ import annotations

from armonik.common import ResultStatus

from pymonik._internal.query import _RESULT_FIELDS, _QueryContext, ResultQuery


class _RecordingResults:
    """Captures the filter handed to each ``list_results`` call."""

    def __init__(self, total: int = 0) -> None:
        self.filters: list = []
        self._total = total

    def list_results(
        self, *, result_filter, page, page_size, sort_direction, sort_field=None
    ):
        self.filters.append(result_filter)
        return self._total, []


class _ExplodingTasks:
    """Any call means the old task-enumeration path is back."""

    def list_tasks(self, *args, **kwargs):
        raise AssertionError("session.results must not enumerate tasks")


def _ctx(scoped_session_id, total: int = 0):
    results = _RecordingResults(total)
    ctx = _QueryContext(
        tasks=_ExplodingTasks(),
        sessions=None,
        results=results,
        partitions=None,
        scoped_session_id=scoped_session_id,
    )
    return ctx, results


def test_session_id_is_a_registered_filter_field():
    # The upstream Result model supports these; PymoniK now exposes them.
    for field in ("session_id", "name", "created_at", "completed_at"):
        assert field in _RESULT_FIELDS


def test_scoped_query_filters_by_session_id_server_side():
    ctx, results = _ctx("sess-xyz")
    ResultQuery(ctx).list()  # raises via _ExplodingTasks if it walks tasks
    assert len(results.filters) == 1
    f = str(results.filters[0])
    assert "SESSION_ID" in f and "sess-xyz" in f


def test_scoped_user_filter_ands_with_session_scope():
    ctx, results = _ctx("sess-xyz")
    ResultQuery(ctx).where(status=ResultStatus.COMPLETED).list()
    f = str(results.filters[0])
    assert "SESSION_ID" in f and "sess-xyz" in f
    assert "STATUS" in f  # the user predicate is still ANDed in


def test_cluster_wide_query_has_no_session_scope():
    ctx, results = _ctx(None)
    ResultQuery(ctx).list()
    assert results.filters == [None]


def test_count_reads_server_side_total_without_walking_tasks():
    ctx, _ = _ctx("sess-xyz", total=7)
    assert ResultQuery(ctx).count() == 7
