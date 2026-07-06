"""``client.session(deps=..., isolate=..., index_url=...)`` sugar.

These flags should land on the session's default TaskOpts so every
submitted envelope picks them up (without each ``@task`` having to
opt in individually).
"""

from __future__ import annotations

from pymonik import task
from pymonik.testing import LocalCluster


@task
def echo(x: int) -> int:
    return x


def test_session_deps_propagate_to_default_options():
    with LocalCluster() as client:
        with client.session(deps=["numpy"]) as s:
            assert s._default_opts.deps == ("numpy",)


def test_session_isolate_false_propagates():
    with LocalCluster() as client:
        with client.session(deps=["numpy"], isolate=False) as s:
            assert s._default_opts.deps == ("numpy",)
            assert s._default_opts.isolate is False


def test_session_index_url_propagates():
    with LocalCluster() as client:
        with client.session(
            deps=["numpy"],
            index_url="https://idx.example/simple/",
        ) as s:
            assert s._default_opts.index_url == "https://idx.example/simple/"


def test_session_no_deps_keeps_empty_opts():
    with LocalCluster() as client:
        with client.session() as s:
            assert s._default_opts.deps is None
            assert s._default_opts.isolate is None
