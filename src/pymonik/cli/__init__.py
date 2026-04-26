"""``pymonik`` CLI.

Standalone executable (``pymonik <subcommand>``); not an ArmoniK CLI
extension. The two are deliberately separate: the host ArmoniK CLI
covers operator verbs (sessions, partitions, results), and ``pymonik``
covers the Python-specific things (worker images, doctor, run/shell
ergonomics, replay) that don't belong in a generic cluster CLI.

Entry point is :func:`pymonik.cli.main.cli`.
"""

from pymonik.cli.main import cli

__all__ = ["cli"]
