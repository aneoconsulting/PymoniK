"""End-to-end runtime-deps tests via ``LocalCluster``.

These tests exercise the real env_builder + subprocess dispatcher against
a real ``uv``. They're slow-ish (the first install takes 10-30s) so we
mark them ``slow`` and skip if ``uv`` is missing on the box.

Run only the fast suite:
    uv run pytest tests/ -m "not slow"

Run with these:
    uv run pytest tests/test_runtime_deps_local.py -v
"""

from __future__ import annotations

import shutil
import sys

import pytest

from pymonik import task
from pymonik.testing import LocalCluster

pytestmark = [
    pytest.mark.skipif(
        shutil.which("uv") is None,
        reason="uv not on PATH; runtime-deps tests need it",
    ),
    pytest.mark.skipif(
        sys.platform == "win32", reason="subprocess wire is POSIX-only for now"
    ),
    pytest.mark.slow,
]


import numpy as np


@task
def numpy_arange_sum(n: int) -> int:
    return int(np.arange(n).sum())


@task(deps=["numpy"])
def per_task_numpy(n: int) -> int:
    return int(np.arange(n).sum())


@task
def imports_unavailable() -> str:
    import numpy  # noqa: F401

    return "ok"


def test_runtime_deps_default_path(tmp_path, monkeypatch):
    """Default ``isolate=False`` — in-process splice."""
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    with LocalCluster() as client:
        with client.session(deps=["numpy"]) as s:
            assert numpy_arange_sum.spawn(100).result(timeout=600) == sum(range(100))


def test_runtime_deps_env_reuse(tmp_path, monkeypatch):
    """Two tasks in the same session — only one install."""
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    with LocalCluster() as client:
        with client.session(deps=["numpy"]) as s:
            a = numpy_arange_sum.spawn(10).result(timeout=600)
            b = numpy_arange_sum.spawn(20).result(timeout=120)
            assert a == sum(range(10))
            assert b == sum(range(20))


def test_runtime_deps_isolate_true_subprocess(tmp_path, monkeypatch):
    """Explicit opt-in ``isolate=True`` — subprocess per task."""
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    with LocalCluster() as client:
        with client.session(deps=["numpy"], isolate=True) as s:
            assert numpy_arange_sum.spawn(50).result(timeout=600) == sum(range(50))


def test_no_deps_no_install(tmp_path, monkeypatch):
    """Sessions without deps must NOT touch the envs root.

    Catches a regression where an empty/None deps list accidentally
    triggers a venv build.
    """
    envs_root = tmp_path / "envs"
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(envs_root))

    @task
    def trivial(x: int) -> int:
        return x * 2

    with LocalCluster() as client:
        with client.session() as s:
            assert trivial.spawn(21).result(timeout=30) == 42

    assert not envs_root.exists() or not any(envs_root.iterdir())


def test_per_task_deps_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    with LocalCluster() as client:
        with client.session() as s:
            assert per_task_numpy.spawn(10).result(timeout=600) == sum(range(10))


def test_ctx_injection_in_isolated_subprocess(tmp_path, monkeypatch):
    """H3 gap: `ctx: pymonik.Ctx` (and current()) work in isolate=True too.

    The detached child has no cancellation/sidecar access, but the parent
    forwards task/session identity via env vars so the read-only context is
    populated.
    """
    import pymonik

    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))

    @task(deps=["numpy"], isolate=True)
    def isolated_ctx(n: int, *, ctx: pymonik.Ctx) -> dict:
        import numpy as _np

        return {
            "sum": int(_np.arange(n).sum()),
            "task_id": ctx.task_id,
            "session_id": ctx.session_id,
            "current_task_id": pymonik.current().task_id,
        }

    with LocalCluster() as client:
        with client.session(deps=["numpy"], isolate=True):
            out = isolated_ctx.spawn(5).result(timeout=600)
    assert out["sum"] == sum(range(5))
    assert out["task_id"] and isinstance(out["task_id"], str)
    assert out["session_id"] and isinstance(out["session_id"], str)
    # current() resolves to the same task inside the subprocess.
    assert out["current_task_id"] == out["task_id"]


def test_install_failure_surfaces_typed_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMONIK_ENVS_ROOT", str(tmp_path / "envs"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    from pymonik.errors import PymonikError

    with LocalCluster() as client:
        with client.session(deps=["this-package-does-not-exist-12345-zzz"]) as s:
            fut = imports_unavailable.spawn()
            with pytest.raises((PymonikError, Exception)) as exc_info:
                fut.result(timeout=120)
            # Surface should contain the failing package name somewhere.
            assert "this-package-does-not-exist" in str(exc_info.value).lower()
