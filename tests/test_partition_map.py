"""partition_map: program partition names -> cluster partitions."""

from __future__ import annotations

import cloudpickle
import pytest

from pymonik import PymonikClient, hooks, task
from pymonik._internal import partitions
from pymonik._internal.submit import submit_many
from pymonik.errors import PymonikError
from pymonik.options import EMPTY
from pymonik.testing import LocalCluster

MAP = {"cpu": "cpu", "gpu-a100": "gpu-a100", "gpu": "gpu-a100", "*": "cpu"}


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks._reset_for_tests()
    yield
    hooks._reset_for_tests()


@task
def plain() -> int:
    return 1


@task(partition="gpu")
def on_gpu() -> int:
    return 2


def test_resolve():
    assert partitions.resolve("gpu", MAP) == "gpu-a100"
    assert partitions.resolve("gpu-a100", MAP) == "gpu-a100"
    assert partitions.resolve("pymonik", MAP) == "cpu"
    assert partitions.resolve("", MAP) == ""  # the session default stays implicit
    assert partitions.resolve("pymonik", {"gpu": "gpu-a100"}) == "pymonik"  # no "*"
    assert partitions.resolve_all(["pymonik", "gpu", "cpu"], MAP) == ("cpu", "gpu-a100")


def test_session_task_and_with_options_are_mapped():
    seen = []
    hooks.on(hooks.TaskSubmitted, lambda ev: seen.append((ev.task_name, ev.partition)))
    with LocalCluster(partition_map=MAP) as c, c.session(partition=["pymonik", "gpu"]) as s:
        assert s.partitions == ("cpu", "gpu-a100")
        futs = [plain.spawn(), on_gpu.spawn(), plain.with_options(partition="metal").spawn()]
        assert [f.result(timeout=10) for f in futs] == [1, 2, 1]
    assert seen == [("plain", "cpu"), ("on_gpu", "gpu-a100"), ("plain", "cpu")]
    assert on_gpu.opts.partition == "gpu"  # the Task object itself is unchanged


def test_partition_map_from_the_environment(monkeypatch):
    monkeypatch.setenv("PYMONIK_PARTITION_MAP", "gpu=gpu-a100, *=cpu")
    with LocalCluster() as c, c.session(partition=["pymonik", "gpu"]) as s:
        assert s.partitions == ("cpu", "gpu-a100")
    client = PymonikClient(endpoint="localhost:1")
    assert client._partition_map == {"gpu": "gpu-a100", "*": "cpu"}
    assert PymonikClient(endpoint="localhost:1", partition_map={})._partition_map == {}


def test_tasks_shipped_with_a_submission_carry_mapped_partitions():
    def body() -> object:
        return on_gpu.with_options(retries=1)  # a task body that would spawn on_gpu

    with partitions.shipping(MAP):
        blob = cloudpickle.dumps(body)
    shipped = cloudpickle.loads(blob)()
    assert shipped.opts.partition == "gpu-a100"
    assert shipped.opts.retries == 1
    assert shipped.name == "on_gpu" and shipped.ctx_param is None
    # outside a submission, nothing is mapped
    assert cloudpickle.loads(cloudpickle.dumps(on_gpu)).opts.partition == "gpu"


def test_validation_uses_mapped_names():
    class _Strict:
        session_id = "s"
        allowed_partitions = ("cpu",)

        def allocate_outputs(self, names):
            raise AssertionError("validation runs first")

    with pytest.raises(PymonikError, match="partition 'gpu-a100'"):
        submit_many(
            task=on_gpu,
            calls=[((), {})],
            backend=_Strict(),  # type: ignore[arg-type]
            blob_uploader=lambda b: "unused",
            spill_threshold=1024,
            default_opts=EMPTY,
            partition="cpu",
            future_factory=lambda *a: None,
            partition_map=MAP,
        )
