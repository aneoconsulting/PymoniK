"""``env`` parameter on session / @task / .with_options.

Covers:
- env without deps → no venv, just env vars applied
- env merges key-wise across session ← @task ← .with_options
- env participates in env_id (different env → different venv when deps present)
- worker-side env restoration after the task runs
"""

from __future__ import annotations

import os

import pytest

from pymonik import task
from pymonik._internal.env_builder import compute_env_id
from pymonik.envelope import EnvSpec
from pymonik.options import EMPTY, TaskOpts
from pymonik.testing import LocalCluster


@task
def read_env(name: str) -> str | None:
    return os.environ.get(name)


def test_env_only_no_deps_applies_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.delenv("PMK_TEST_KEY", raising=False)
    with LocalCluster() as client:
        with client.session(env={"PMK_TEST_KEY": "from_session"}) as s:
            assert read_env.spawn("PMK_TEST_KEY").result(timeout=30) == "from_session"
    # Restored after the task finishes.
    assert os.environ.get("PMK_TEST_KEY") is None


def test_env_per_task_overrides_session(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.delenv("PMK_TEST_KEY", raising=False)
    overridden = read_env.with_options(env={"PMK_TEST_KEY": "from_task"})
    with LocalCluster() as client:
        with client.session(env={"PMK_TEST_KEY": "from_session"}) as s:
            assert overridden.spawn("PMK_TEST_KEY").result(timeout=30) == "from_task"


def test_env_merge_keywise():
    a = TaskOpts(env={"A": "1", "B": "1"})
    b = TaskOpts(env={"B": "2", "C": "3"})
    merged = a.merge(b)
    assert merged.env == {"A": "1", "B": "2", "C": "3"}


def test_env_changes_env_id_when_deps_present():
    base = EnvSpec(deps=("numpy",))
    with_env = EnvSpec(deps=("numpy",), env=(("FOO", "1"),))
    assert compute_env_id(base) != compute_env_id(with_env)


def test_env_id_stable_for_same_env_unsorted():
    """``submit_many`` always sorts before building the spec, but verify
    the hash itself doesn't depend on the input order."""
    a = EnvSpec(deps=("numpy",), env=(("A", "1"), ("B", "2")))
    b = EnvSpec(deps=("numpy",), env=(("B", "2"), ("A", "1")))
    assert compute_env_id(a) == compute_env_id(b)
