"""Typed worker-context injection — ``ctx: pymonik.Ctx`` (RFC §6.4, H3).

A parameter annotated ``pymonik.Ctx`` (alias of ``WorkerContext``) is
detected at decoration and the live context is injected by the worker at
dispatch. The ContextVar form (``pymonik.current()``) keeps working too.
"""

from __future__ import annotations

import pytest

import pymonik
from pymonik import task
from pymonik.testing import LocalCluster


@task
def needs_ctx(x: int, *, ctx: pymonik.Ctx) -> dict:
    return {
        "x": x,
        "task_id": ctx.task_id,
        "session_id": ctx.session_id,
        "attempt": ctx.attempt,
    }


@task
def uses_current(x: int) -> str:
    return pymonik.current().task_id


def test_ctx_param_detected_at_decoration():
    assert needs_ctx.ctx_param == "ctx"
    assert uses_current.ctx_param is None


def test_ctx_injected_by_annotation():
    with LocalCluster() as client:
        with client.session():
            out = needs_ctx.spawn(7).result(timeout=15)
    assert out["x"] == 7
    assert isinstance(out["task_id"], str) and out["task_id"]
    assert isinstance(out["session_id"], str) and out["session_id"]
    assert out["attempt"] == 1


def test_ctx_param_rejected_when_passed_by_caller():
    with LocalCluster() as client:
        with client.session():
            with pytest.raises(pymonik.PymonikError, match="ctx"):
                needs_ctx.spawn(1, ctx="nope")


def test_current_still_works():
    with LocalCluster() as client:
        with client.session():
            tid = uses_current.spawn(3).result(timeout=15)
    assert isinstance(tid, str) and tid
