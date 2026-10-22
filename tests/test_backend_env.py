"""PYMONIK_BACKEND=local: the same program runs on LocalCluster."""

from __future__ import annotations

import pytest

from pymonik import PymonikClient, task
from pymonik.testing import LocalCluster


@task
def add(a: int, b: int) -> int:
    return a + b


def test_local_backend_runs_an_unchanged_program(monkeypatch):
    monkeypatch.setenv("PYMONIK_BACKEND", "local")
    monkeypatch.delenv("AKCONFIG", raising=False)
    client = PymonikClient()
    assert isinstance(client, LocalCluster)
    with client as c, c.session(partition="pymonik"):
        assert add.spawn(2, 3).result(timeout=10) == 5


def test_local_backend_ignores_connection_arguments(monkeypatch):
    monkeypatch.setenv("PYMONIK_BACKEND", "local")
    assert isinstance(PymonikClient(endpoint="nowhere:5001", events=False), LocalCluster)


def test_local_cluster_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("PYMONIK_LOCAL_WORKERS", "3")
    monkeypatch.setenv("PYMONIK_LOCAL_PARTITION_WORKERS", "gpu=1, io=2")
    c = LocalCluster()
    assert c._max_workers == 3
    assert c._partition_workers == {"gpu": 1, "io": 2}
    assert LocalCluster(max_workers=5, partition_workers={})._partition_workers == {}


def test_default_backend_is_armonik(monkeypatch):
    monkeypatch.delenv("PYMONIK_BACKEND", raising=False)
    monkeypatch.delenv("AKCONFIG", raising=False)
    with pytest.raises(ValueError, match="AKCONFIG"):
        PymonikClient()


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setenv("PYMONIK_BACKEND", "cloud")
    with pytest.raises(ValueError, match="PYMONIK_BACKEND"):
        PymonikClient()
