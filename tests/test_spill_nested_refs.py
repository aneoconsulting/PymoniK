"""Regression: auto-spill of a container holding nested refs must still
resolve those refs on the worker.

The bug: a container (list/dict) large enough to trip auto-spill was
uploaded as one ENC_PICKLE blob with the nested ``FutureRef`` pickled
inside it. The worker's ``resolve_refs`` handled the resulting top-level
``BlobRef`` by unpickling and returning the container *without
recursing*, so the inner ``FutureRef`` reached the task unresolved — a
silent wrong result (the function saw a ``FutureRef`` object, not the
upstream value). See ``_internal/refs.py`` (``auto_spill`` / ``resolve_refs``).

``LocalSession`` runs the same submit + ref-resolution pipeline as the
cluster session, so forcing a tiny spill threshold reproduces it
deterministically without a real cluster.
"""

from __future__ import annotations

from pymonik import task
from pymonik.testing import LocalCluster

# Comfortably above the forced threshold (256 B) below, so any container
# carrying it spills as a single blob.
_PADDING = b"x" * 4096


@task
def produce() -> int:
    return 42


@task
def consume_list(payload: list) -> object:
    # Must be the resolved upstream value (42), not a FutureRef sentinel.
    return payload[0]


@task
def consume_dict(payload: dict) -> object:
    return payload["fut"]


def _force_spill(session, threshold: int = 256) -> None:
    # LocalSession defaults to a ~1 GiB threshold (never spills); shrink it
    # so a small container trips the spill path the cluster would hit on a
    # genuinely large arg.
    session._spill_threshold = threshold


def test_spilled_list_resolves_nested_future() -> None:
    with LocalCluster() as client:
        with client.session() as s:
            _force_spill(s)
            producer = produce.spawn()
            got = consume_list.spawn([producer, _PADDING]).result()
    assert got == 42


def test_spilled_dict_resolves_nested_future() -> None:
    with LocalCluster() as client:
        with client.session() as s:
            _force_spill(s)
            producer = produce.spawn()
            got = consume_dict.spawn({"fut": producer, "pad": _PADDING}).result()
    assert got == 42


def test_inline_container_still_resolves() -> None:
    # Control: below threshold, no spill. Nested ref resolves via the normal
    # container walk (this worked before the fix too) — guards against a fix
    # that only ever runs on the spill path.
    with LocalCluster() as client:
        with client.session() as s:
            producer = produce.spawn()
            got = consume_list.spawn([producer, b"x" * 16]).result()
    assert got == 42
