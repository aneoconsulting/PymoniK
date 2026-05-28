"""Top-level ``pymonik`` Click app.

Subcommand layout (current state — many are stubs):

    pymonik doctor          # AKCONFIG + reachability + cluster version checks
    pymonik run script.py   # ergonomic submitter (stub)
    pymonik shell           # IPython repl bound to a session (stub)
    pymonik image build     # bake a worker image from uv.lock (stub)
    pymonik image push      # push to a registry (stub)
    pymonik image list      # show baked images (stub)
    pymonik logs <task>     # stream worker stdout/stderr (stub)
    pymonik replay <task>   # local replay of a failed task (stub)

The doctor command is wired up; the rest are scaffolded so users can
discover them via ``pymonik --help`` and so future PRs land in a
predictable place.
"""

from __future__ import annotations

import rich_click as click

from pymonik.cli.doctor import doctor as _doctor_cmd


@click.group(
    name="pymonik",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(package_name="pymonik", message="pymonik %(version)s")
def cli() -> None:
    """Python-side companion CLI for ArmoniK.

    For session / partition / result operator verbs, use the host
    ``armonik`` CLI. ``pymonik`` covers Python-specific tooling: worker
    images, doctor checks, run/shell ergonomics, replay.
    """


# ---------- top-level commands ----------

cli.add_command(_doctor_cmd)


@cli.command("run")
@click.argument("script", type=click.Path(exists=True, dir_okay=False))
@click.option("--partition", default=None, help="ArmoniK partition.")
def run_script(script: str, partition: str | None) -> None:
    """[stub] Run a Python script with a default Pymonik session open.

    Will eventually wrap the script in ``with PymonikClient() as client:
    with client.session(partition=...): ...`` so the user doesn't have
    to. Today: not implemented.
    """
    click.echo("pymonik run: not yet implemented", err=True)
    raise SystemExit(2)


@cli.command("shell")
@click.option("--partition", default=None, help="ArmoniK partition.")
def shell(partition: str | None) -> None:
    """[stub] Open an IPython REPL with a session pre-opened."""
    click.echo("pymonik shell: not yet implemented", err=True)
    raise SystemExit(2)


@cli.group("image")
def image_group() -> None:
    """[stub] Worker image management."""


@image_group.command("build")
def image_build() -> None:
    """[stub] Bake a worker image from ``uv.lock`` (Image.from_uv)."""
    click.echo("pymonik image build: not yet implemented", err=True)
    raise SystemExit(2)


@image_group.command("push")
def image_push() -> None:
    """[stub] Push a built worker image to a registry."""
    click.echo("pymonik image push: not yet implemented", err=True)
    raise SystemExit(2)


@image_group.command("list")
def image_list() -> None:
    """[stub] List baked worker images."""
    click.echo("pymonik image list: not yet implemented", err=True)
    raise SystemExit(2)


@cli.command("logs")
@click.argument("task_id")
@click.option("--follow", "-f", is_flag=True, help="Stream new log lines.")
def logs(task_id: str, follow: bool) -> None:
    """[stub] Stream a task's worker stdout/stderr."""
    click.echo("pymonik logs: not yet implemented", err=True)
    raise SystemExit(2)


@cli.command("replay")
@click.argument("task_id")
def replay(task_id: str) -> None:
    """[stub] Re-run a failed task locally with its captured inputs."""
    click.echo("pymonik replay: not yet implemented", err=True)
    raise SystemExit(2)


@cli.group("cache")
def cache_group() -> None:
    """Local execution cache management.

    See ``PymonikClient(cache=...)`` for what enables it. By default the
    cache lives at ``~/.cache/pymonik`` (or ``$XDG_CACHE_HOME/pymonik``
    when set).
    """


@cache_group.command("path")
@click.option(
    "--root",
    type=click.Path(),
    default=None,
    help="Override the cache root (otherwise ~/.cache/pymonik / XDG).",
)
def cache_path(root: str | None) -> None:
    """Print the cache directory path."""
    from pathlib import Path

    from pymonik._internal.exec_cache import default_cache_dir

    p = Path(root) if root else default_cache_dir()
    click.echo(str(p))


@cache_group.command("stats")
@click.option(
    "--root",
    type=click.Path(),
    default=None,
    help="Override the cache root.",
)
def cache_stats(root: str | None) -> None:
    """Show entry count and total bytes in the cache."""
    from pathlib import Path

    from pymonik._internal.exec_cache import ExecCache, default_cache_dir

    p = Path(root) if root else default_cache_dir()
    if not p.exists():
        click.echo(f"  cache not present at {p}")
        return
    cache = ExecCache(p)
    s = cache.stats()
    click.echo(f"  root      {p}")
    click.echo(f"  entries   {s['entries']}")
    mb = s["bytes"] / (1024 * 1024)
    click.echo(f"  size      {s['bytes']} bytes ({mb:.2f} MiB)")


@cache_group.command("clear")
@click.option(
    "--root",
    type=click.Path(),
    default=None,
    help="Override the cache root.",
)
@click.confirmation_option(
    prompt="Delete every cached task result?",
    help="Skip the prompt.",
)
def cache_clear(root: str | None) -> None:
    """Delete every cached entry under the cache root."""
    from pathlib import Path

    from pymonik._internal.exec_cache import ExecCache, default_cache_dir

    p = Path(root) if root else default_cache_dir()
    if not p.exists():
        click.echo(f"  cache not present at {p}")
        return
    cache = ExecCache(p)
    n = cache.clear()
    click.echo(f"  cleared {n} entries from {p}")


if __name__ == "__main__":
    cli()
