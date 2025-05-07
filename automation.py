# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mkdocs-material[imaging]",
#     "rich-click",
#     "ruff",
# ]
# ///

import rich_click as click
import subprocess
import shutil
import os
from pathlib import Path

# TODO: Consider switching to zxpy
# Configure rich-click to use Rich for help text and styling
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_ERRORS_SUGGESTION = "magenta italic"
click.rich_click.ERRORS_SUGGESTION = "Try running the --help flag for more information."
click.rich_click.ERRORS_EPILOGUE = "To find out more, read our [link=https://aneoconsulting.github.io/PymoniK/]developer's guide[/link]"

@click.group()
def cli():
    """
    A CLI for managing common development tasks for Pymonik.
    """
    pass

@cli.command("build-docker")
@click.option(
    "--image-name",
    "-i",
    default="pymonik_worker",
    show_default=True,
    help="Name of the Docker image to build.",
)
@click.option(
    "--python-version",
    "-pv",
    default="3.10.12",
    show_default=True,
    help="Python version to use in the Docker image.",
)
@click.option(
    "--dockerfile-path",
    "-df",
    default="pymonik_worker/Dockerfile", # Assuming this is the path
    show_default=True,
    help="Path to the Dockerfile relative to the current directory.",
)
@click.option(
    "--context-path",
    "-c",
    default=".",
    show_default=True,
    help="Build context path for Docker.",
)
@click.option(
    "--push",
    is_flag=True,
    help="Push the image to Docker Hub after a successful build.",
)
def build_docker(image_name: str, python_version: str, dockerfile_path: str, context_path: str, push: bool):
    """
    Builds a Docker image.

    Example:
        `python your_script_name.py build-docker -i my_image --python-version 3.11 --push`
    """
    click.secho(f"Building Docker image '{image_name}' with Python {python_version}...", fg="cyan")

    # Check if Dockerfile exists
    if not Path(dockerfile_path).exists():
        click.secho(f"Error: Dockerfile not found at '{dockerfile_path}'. Please specify the correct path.", fg="red")
        raise click.Abort()

    build_command = [
        "docker",
        "build",
        "-t",
        image_name,
        "-f",
        dockerfile_path,
        "--build-arg",
        f"USE_PYTHON_VERSION={python_version}",
        context_path,
    ]

    try:
        click.secho(f"Running command: {' '.join(build_command)}", fg="yellow")
        subprocess.run(build_command, check=True)
        click.secho(f"Docker image '{image_name}' built successfully.", fg="green")

        if push:
            click.secho(f"Pushing image '{image_name}' to Docker Hub...", fg="cyan")
            push_command = ["docker", "push", image_name]
            click.secho(f"Running command: {' '.join(push_command)}", fg="yellow")
            subprocess.run(push_command, check=True)
            click.secho(f"Image '{image_name}' pushed successfully.", fg="green")

    except subprocess.CalledProcessError as e:
        click.secho(f"Error during Docker operation: {e}", fg="red")
        click.secho(f"Command output:\n{e.stdout}\n{e.stderr}", fg="red")
        raise click.Abort()
    except FileNotFoundError:
        click.secho("Error: Docker command not found. Is Docker installed and in your PATH?", fg="red")
        raise click.Abort()


@cli.command("serve-docs")
@click.option(
    "--port",
    "-p",
    default=8000,
    show_default=True,
    help="Port to serve the documentation on.",
    type=int,
)
def serve_docs(port: int):
    """
    Serves the MkDocs documentation locally.
    Requires MkDocs to be installed.
    """
    click.secho(f"Serving MkDocs documentation on http://127.0.0.1:{port}...", fg="cyan")
    command = ["mkdocs", "serve", "--dev-addr", f"127.0.0.1:{port}"]
    try:
        click.secho(f"Running command: {' '.join(command)}", fg="yellow")
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        click.secho(f"Error serving documentation: {e}", fg="red")
        raise click.Abort()
    except FileNotFoundError:
        click.secho("Error: mkdocs command not found. Is MkDocs installed and in your PATH?", fg="red")
        raise click.Abort()


@cli.command("publish-docs")
@click.option(
    "--message",
    "-m",
    help="Commit message for publishing the documentation.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force push the documentation. Use with caution.",
)
def publish_docs(message: str | None, force: bool):
    """
    Builds and deploys the MkDocs documentation, typically to GitHub Pages.
    This command uses `mkdocs gh-deploy`.
    """
    click.secho("Publishing MkDocs documentation...", fg="cyan")
    command = ["mkdocs", "gh-deploy"]
    if message:
        command.extend(["--message", message])
    if force:
        command.append("--force")

    try:
        click.secho(f"Running command: {' '.join(command)}", fg="yellow")
        subprocess.run(command, check=True)
        click.secho("Documentation published successfully.", fg="green")
    except subprocess.CalledProcessError as e:
        click.secho(f"Error publishing documentation: {e}", fg="red")
        raise click.Abort()
    except FileNotFoundError:
        click.secho("Error: mkdocs command not found. Is MkDocs installed and in your PATH?", fg="red")
        raise click.Abort()


@cli.command("publish-project")
@click.option(
    "--project-dir",
    "-d",
    default="./pymonik",
    show_default=True,
    help="Path to the Python project directory managed by UV.",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
)
@click.option(
    "--token",
    envvar="UV_PYPI_TOKEN", # Example environment variable, adjust as needed
    help="Authentication token for publishing. Can also be set via environment variable (e.g., UV_PYPI_TOKEN).",
)
def publish_project(project_dir: str, token: str | None):
    """
    Builds and publishes a Python project using UV.
    Assumes necessary environment variables for authentication are set if --token is not provided.
    """
    click.secho(f"Publishing Python project in '{project_dir}' using UV...", fg="cyan")

    # Step 1: Ensure the project is built (create sdist and wheel)
    build_command = ["uv", "build"]
    click.secho(f"Running build command in {project_dir}: {' '.join(build_command)}", fg="yellow")
    try:
        subprocess.run(build_command, cwd=project_dir, check=True)
        click.secho("Project built successfully.", fg="green")
    except subprocess.CalledProcessError as e:
        click.secho(f"Error building project with UV: {e}", fg="red")
        click.secho(f"Command output:\n{e.stdout}\n{e.stderr}", fg="red")
        raise click.Abort()
    except FileNotFoundError:
        click.secho("Error: uv command not found. Is UV installed and in your PATH?", fg="red")
        raise click.Abort()

    # Publish the built distributions
    publish_command = ["uv", "publish"]

    publish_env = os.environ.copy()
    if token:
        publish_env["UV_PUBLISH_TOKEN"] = token
        click.secho("Using provided token for publishing.", fg="yellow")
        click.secho(f"Running publish command: {' '.join(publish_command)}", fg="yellow")
        try:
            subprocess.run(publish_command, cwd=project_dir, check=True, env=publish_env)
            click.secho(f"Successfully published project to PyPI.", fg="green")
        except subprocess.CalledProcessError as e:
            click.secho(f"Error publishing project with UV: {e}", fg="red")
            click.secho(f"Command output:\n{e.stdout}\n{e.stderr}", fg="red")
        except FileNotFoundError:
            click.secho("Error: uv command not found. Is UV installed and in your PATH?", fg="red")
            raise click.Abort()
        finally:
            # Delete the dist folder
            dist_path = Path(project_dir) / "dist"
            if dist_path.exists() and dist_path.is_dir():
                try:
                    shutil.rmtree(dist_path)
                    click.secho(f"Successfully deleted directory: {dist_path}", fg="green")
                except OSError as e:
                    click.secho(f"Error deleting directory {dist_path}: {e}", fg="red")
            else:
                click.secho(f"Directory not found (or not a directory): {dist_path}", fg="yellow")
    click.secho("Project publishing process completed.", fg="green")


@cli.command("clean")
def clean():
    """
    Cleans the project:
    - Deletes the 'site/' directory (MkDocs build output).
    - Cleans UV projects in 'pymonik/' and 'test_client/' by removing common build artifacts.
    """
    click.secho("Cleaning project...", fg="cyan")

    # Delete site/ directory
    site_dir = Path("site")
    if site_dir.exists() and site_dir.is_dir():
        try:
            shutil.rmtree(site_dir)
            click.secho(f"Successfully deleted directory: {site_dir}", fg="green")
        except OSError as e:
            click.secho(f"Error deleting directory {site_dir}: {e}", fg="red")
    else:
        click.secho(f"Directory not found (or not a directory): {site_dir}", fg="yellow")

    # Clean specified UV project directories
    project_dirs_to_clean = ["pymonik", "test_client"]
    for project_path_str in project_dirs_to_clean:
        project_path = Path(project_path_str)
        click.secho(f"Cleaning UV project in '{project_path}'...", fg="cyan")
        if project_path.exists() and project_path.is_dir():
            # Common directories/files to remove for a "clean" operation
            # `uv clean` itself is more about the global cache.
            # For project cleaning, we remove typical build/cache outputs.
            items_to_remove = [
                ".venv",
                "__pycache__",
                ".pytest_cache",
                "build",
                "dist",
                "*.egg-info", # Glob pattern for .egg-info directories
                ".ruff_cache",
                ".mypy_cache"
            ]

            for item_name in items_to_remove:
                if "*" in item_name: # Handle glob patterns
                    for matching_item in project_path.glob(item_name):
                        try:
                            if matching_item.is_dir():
                                shutil.rmtree(matching_item)
                                click.secho(f"  Removed directory: {matching_item}", fg="green")
                            elif matching_item.is_file():
                                matching_item.unlink()
                                click.secho(f"  Removed file: {matching_item}", fg="green")
                        except OSError as e:
                            click.secho(f"  Error removing {matching_item}: {e}", fg="red")
                else:
                    item_path = project_path / item_name
                    if item_path.exists():
                        try:
                            if item_path.is_dir():
                                shutil.rmtree(item_path)
                                click.secho(f"  Removed directory: {item_path}", fg="green")
                            elif item_path.is_file():
                                item_path.unlink()
                                click.secho(f"  Removed file: {item_path}", fg="green")
                        except OSError as e:
                            click.secho(f"  Error removing {item_path}: {e}", fg="red")
                    else:
                        click.secho(f"  Item not found: {item_path}", fg="yellow")
            click.secho(f"Cleaning for '{project_path}' complete.", fg="green")
        else:
            click.secho(f"Directory not found for cleaning: {project_path}", fg="yellow")


    click.secho("Project cleaning finished.", fg="green")

# TODO: format command

if __name__ == "__main__":
    cli()
