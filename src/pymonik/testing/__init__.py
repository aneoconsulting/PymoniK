"""In-process backend for unit tests, examples, and demos.

Usage:

    from pymonik.testing import LocalCluster

    with LocalCluster() as client:
        with client.session(partition="local") as s:
            assert add.spawn(2, 3).result() == 5

See :class:`LocalCluster` for what's supported and the few cluster-only
features that are no-ops (``pause`` / ``resume`` / ``stop_submission``
have no in-process meaning and just log).
"""

from pymonik.testing.local import LocalCluster, LocalSession

__all__ = ["LocalCluster", "LocalSession"]
