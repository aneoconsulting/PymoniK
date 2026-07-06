"""Shared test fixtures.

``cluster_client`` provides a connected :class:`PymonikClient` for e2e
tests that need a real ArmoniK cluster. It SKIPS (never fails) when no
cluster is configured, so the default ``pytest`` run stays hermetic:

    # hermetic (default) — e2e tests skip:
    uv run pytest -m "not e2e"
    # against a cluster:
    export AKCONFIG=/path/to/generated/armonik-cli.yaml
    uv run pytest -m e2e
"""

from __future__ import annotations

import os

import pytest

# Partition the e2e workload submits into; override per cluster.
E2E_PARTITION = os.environ.get("PYMONIK_E2E_PARTITION", "pymonikv1")


@pytest.fixture(scope="session")
def cluster_client():
    if not (os.environ.get("AKCONFIG") or os.environ.get("PYMONIK_ENDPOINT")):
        pytest.skip("e2e: set AKCONFIG (or PYMONIK_ENDPOINT) to run against a cluster")
    from pymonik import PymonikClient

    client = PymonikClient(endpoint=os.environ.get("PYMONIK_ENDPOINT"))
    try:
        client.__enter__()
        # Cheap reachability probe — skip rather than hang/fail if the
        # endpoint is set but nothing is listening.
        client.sessions.limit(1).list()
    except Exception as e:  # noqa: BLE001
        try:
            client.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        pytest.skip(f"e2e: ArmoniK cluster not reachable: {e!r}")
    yield client
    client.__exit__(None, None, None)
