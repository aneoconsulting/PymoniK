"""Wire round-trip for ``EnvSpec`` on the envelope."""

from __future__ import annotations

from pymonik.envelope import EnvSpec, TaskEnvelope, decode, encode


def _roundtrip(env: TaskEnvelope) -> TaskEnvelope:
    return decode(encode(env))


def test_envelope_without_env_spec_default_none():
    env = TaskEnvelope(function_pickle=b"f", args_pickle=b"a", func_name="t")
    rt = _roundtrip(env)
    assert rt.env_spec is None


def test_envelope_with_env_spec_roundtrips():
    spec = EnvSpec(deps=("numpy>=2", "polars"), isolate=True, index_url="")
    env = TaskEnvelope(
        function_pickle=b"f",
        args_pickle=b"a",
        func_name="t",
        env_spec=spec,
    )
    rt = _roundtrip(env)
    assert rt.env_spec is not None
    assert rt.env_spec.deps == ("numpy>=2", "polars")
    assert rt.env_spec.isolate is True
    assert rt.env_spec.index_url == ""


def test_envelope_with_isolate_false_roundtrips():
    spec = EnvSpec(deps=("numpy",), isolate=False)
    env = TaskEnvelope(
        function_pickle=b"f", args_pickle=b"a", func_name="t", env_spec=spec
    )
    rt = _roundtrip(env)
    assert rt.env_spec is not None
    assert rt.env_spec.isolate is False
