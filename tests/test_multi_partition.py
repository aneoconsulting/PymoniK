"""Multi-partition routing on ``client.session(partition=[...])``."""

from __future__ import annotations

import pytest

from pymonik import task
from pymonik.errors import PymonikError
from pymonik.testing import LocalCluster


@task
def add(a: int, b: int) -> int:
    return a + b


def test_session_accepts_partition_string():
    with LocalCluster() as client:
        with client.session(partition="cpu") as s:
            assert s.partitions == ("cpu",)
            assert s.partition == "cpu"


def test_session_accepts_partition_list():
    with LocalCluster() as client:
        with client.session(partition=["cpu", "gpu", "io"]) as s:
            assert s.partitions == ("cpu", "gpu", "io")
            assert s.partition == "cpu"  # first is default


def test_empty_partition_list_rejected():
    with LocalCluster() as client:
        with pytest.raises(ValueError, match="partition list cannot be empty"):
            client.session(partition=[])


def test_per_task_partition_within_set_succeeds():
    """LocalBackend has no partition constraint, so any selection is fine
    in-process. Cluster-level enforcement happens via the ``allowed_partitions``
    backend hook on the real ``Session._ClientBackend``."""
    with LocalCluster() as client:
        with client.session(partition=["cpu", "gpu"]) as s:
            override = add.with_options(partition="gpu")
            assert override.spawn(2, 3).result(timeout=10) == 5


def test_per_task_partition_outside_set_rejected_by_cluster_backend():
    """The local backend reports ``allowed_partitions=None`` so it doesn't
    enforce; this test reaches into the submission pipeline directly to
    verify the validation logic with a backend that *does* report a set.
    """
    from armonik.common import TaskDefinition, TaskOptions

    from pymonik._internal.submit import submit_many
    from pymonik.future import Future

    class _StrictBackend:
        @property
        def session_id(self) -> str:
            return "test"

        @property
        def allowed_partitions(self) -> tuple[str, ...]:
            return ("cpu",)

        def allocate_outputs(self, names):
            raise AssertionError("should not be called — validation runs first")

        def upload_payloads(self, named):
            raise AssertionError("should not be called")

        def submit(self, defs, opts):
            raise AssertionError("should not be called")

    bad = add.with_options(partition="gpu")
    with pytest.raises(PymonikError, match="partition 'gpu'"):
        submit_many(
            task=bad,
            calls=[((1, 2), {})],
            backend=_StrictBackend(),
            blob_uploader=lambda b: "ignored",
            spill_threshold=1024,
            default_opts=type(bad.opts)(),
            partition="cpu",
            future_factory=lambda *a, **k: AssertionError("unreached"),
        )
