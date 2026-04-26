"""``pymonik doctor`` — local-side health check.

Verifies the things a new user would otherwise hit as cryptic failures:

- AKCONFIG is set / readable / parseable.
- Endpoint is reachable (gRPC channel opens within a deadline).
- Versions: pymonik / armonik / Python / cluster (via ``ArmoniKVersions``).
- Listed partitions on the cluster (sanity-check that the user's
  ``--partition`` actually exists before they submit).

Exits 0 on full green, non-zero with a one-line summary otherwise.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click


@click.command("doctor")
@click.option(
    "--akconfig",
    type=click.Path(dir_okay=False),
    default=None,
    help="Path to the ArmoniK CLI YAML config. Falls back to $AKCONFIG.",
)
@click.option(
    "--endpoint",
    default=None,
    help="Endpoint override (e.g. http://10.43.x.y:5001). Skips AKCONFIG.",
)
@click.option(
    "--timeout",
    default=5.0,
    show_default=True,
    type=float,
    help="Connection deadline in seconds.",
)
def doctor(akconfig: str | None, endpoint: str | None, timeout: float) -> None:
    """Check the local config + reach the cluster + report versions."""
    failures: list[str] = []

    # ---- pymonik / armonik / python versions ----
    try:
        import pymonik

        click.echo(f"  pymonik       {pymonik.__version__}")
    except Exception as e:  # pragma: no cover — defensive
        failures.append(f"pymonik import: {e!r}")

    try:
        import armonik
        from importlib.metadata import version as _ver

        click.echo(f"  armonik (py)  {_ver('armonik')}")
    except Exception as e:
        failures.append(f"armonik import: {e!r}")

    click.echo(
        f"  python        {sys.version.split()[0]}  "
        f"({sys.implementation.name})"
    )

    # ---- AKCONFIG / endpoint resolution ----
    cfg_endpoint: str | None = None
    cfg_ca: str | None = None

    if endpoint is not None:
        cfg_endpoint = endpoint
        click.echo(f"  endpoint      {endpoint}  (--endpoint override)")
    else:
        cfg_path = akconfig or os.getenv("AKCONFIG")
        if cfg_path is None:
            failures.append(
                "no endpoint: pass --endpoint or set AKCONFIG to your "
                "armonik-cli.yaml"
            )
        else:
            try:
                from pymonik.client import _load_akconfig

                loaded = _load_akconfig(Path(cfg_path))
                cfg_endpoint = loaded["endpoint"]
                cfg_ca = loaded.get("certificate_authority")
                click.echo(f"  AKCONFIG      {cfg_path}")
                click.echo(f"  endpoint      {cfg_endpoint}")
                if cfg_ca:
                    click.echo(f"  ca cert       {cfg_ca}")
            except Exception as e:
                failures.append(f"AKCONFIG load: {e!r}")

    # ---- channel reachability ----
    if cfg_endpoint is not None and not failures:
        try:
            import grpc
            from pymonik._internal.channel import Credentials, open_channel

            creds = (
                Credentials(ca=cfg_ca) if cfg_ca else None
            )
            channel = open_channel(cfg_endpoint, creds)
            try:
                grpc.channel_ready_future(channel).result(timeout=timeout)
                click.echo(f"  channel       ready (≤{timeout}s)")
            except Exception as e:
                failures.append(f"channel not ready within {timeout}s: {e!r}")
            finally:
                # Try cluster-side queries before tearing down.
                if not failures:
                    _query_cluster(channel, failures)
                channel.close()
        except Exception as e:
            failures.append(f"channel open failed: {e!r}")

    # ---- summary ----
    click.echo("")
    if failures:
        click.echo(click.style("FAIL", fg="red", bold=True))
        for f in failures:
            click.echo(f"  - {f}")
        raise SystemExit(1)
    click.echo(click.style("OK", fg="green", bold=True))


def _query_cluster(channel: Any, failures: list[str]) -> None:
    """Best-effort cluster-side queries: versions, partitions."""
    try:
        from armonik.client import ArmoniKPartitions, ArmoniKVersions

        try:
            v = ArmoniKVersions(channel)
            versions = v.list_versions()
            click.echo(f"  cluster       core={versions.get('core', '?')} "
                       f"api={versions.get('api', '?')}")
        except Exception as e:
            click.echo(f"  cluster       (versions: {e!r})")

        try:
            p = ArmoniKPartitions(channel)
            total, items = p.list_partitions(page=0, page_size=50)
            names = [getattr(it, "id", str(it)) for it in items]
            click.echo(
                f"  partitions    {total} total: "
                f"{', '.join(names) if names else '(none listed)'}"
            )
        except Exception as e:
            click.echo(f"  partitions    (failed: {e!r})")
    except Exception as e:
        failures.append(f"cluster query setup: {e!r}")
