from logging import Logger
import subprocess
import sys
import os
from typing import Any, Dict
import importlib


class RuntimeEnvironment:
    """
    A class to manage the runtime environment for Python packages.
    """

    def __init__(self, logger: Logger = None):
        self.logger = logger
        self.python_executable = sys.executable
        self.venv_path = os.path.dirname(self.python_executable)
        self.pip_executable = (
            os.path.join(self.venv_path, "Scripts", "pip")
            if os.name == "nt"
            else os.path.join(self.venv_path, "bin", "pip")
        )

    def get_python_executable(self):
        return self.python_executable

    def get_venv_path(self):
        return self.venv_path

    def get_pip_executable(self):
        return self.pip_executable

    def install_package(self, package_name: str, version: str = None):
        """
        Installs a Python package using uv.
        Args:
            package_name: The name of the package to install.
            version: Optional specific version string (e.g., '==1.2.3', '>=1.0').
        """
        # Check if uv is callable first (If this fails )
        try:
            subprocess.run(["uv", "--version"], check=True, capture_output=True)
            self.logger.info("uv command found.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.logger.error(
                "uv command not found on PATH. Cannot install packages dynamically this way."
            )
            return False

        self.logger.info(
            f"\nAttempting to install {package_name}{version or ''} using uv..."
        )

        package_spec = package_name
        if version:
            # Basic check - might need adjustment based on uv's exact specifier support
            if not all(
                c.isalnum() or c in [".", "=", "<", ">", "!", "~"] for c in version
            ):  # Added ~ for compatible releases
                self.logger.error(
                    f"Error: Potentially invalid characters in version specifier for uv: {version}"
                )
                # return False # Decide if you want to block or let uv handle potential errors
            package_spec += version

        # --- Use uv command directly ---
        # Note: uv pip install runs in the context of the current environment
        # if a venv is active or detected, similar to pip.
        command = ["uv", "pip", "install", package_spec]

        self.logger.info(f"Running command: {' '.join(command)}")

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                env=os.environ.copy(),
                # Consider specifying the target python/venv if needed, though uv
                # usually detects the active one correctly.
                # Example: command = ['uv', 'pip', 'install', package_spec, '--python', sys.executable]
            )

            self.logger.info(f"Successfully installed {package_spec} using uv")
            self.logger.debug(f"Install STDOUT:\n{process.stdout}")
            self.logger.debug(
                f"Install STDERR:\n{process.stderr}"
            )  # uv might output progress here

            # ... (rest of the importlib reload logic remains the same) ...
            try:
                module_name_import = package_name.replace("-", "_")
                module = importlib.import_module(module_name_import)
                importlib.reload(module)
                self.logger.info(f"Module '{module_name_import}' reloaded/available.")
            except ImportError:
                self.logger.warning(
                    f"Could not import '{module_name_import}' immediately after install. May require script restart."
                )
            except Exception as e:
                self.logger.error(f"Error reloading module {module_name_import}: {e}")

            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"Error installing {package_spec} using uv; command failed."
            )
            self.logger.error(f"Return Code: {e.returncode}")
            self.logger.error(f"STDOUT:\n{e.stdout}")
            self.logger.error(
                f"STDERR:\n{e.stderr}"
            )  # This should contain the uv error
            return False
        except Exception as e:
            self.logger.error(
                f"An unexpected error occurred during uv installation setup: {e}"
            )
            return False

    def construct_environment(self, environment_info: Dict[str, Any]):
        """
        Constructs the runtime environment for the Python packages.
        """
        self.logger.info(f"Constructing runtime environment {environment_info}...")
        if "pip" in environment_info:
            pip_info = environment_info["pip"]
            if isinstance(pip_info, list):
                for package in pip_info:
                    if isinstance(package, str):
                        self.install_package(package)
                    elif isinstance(package, tuple):  # Tuple maybe ?
                        for package_name, version in package.items():
                            self.install_package(package_name, version)
                    else:
                        self.logger.error(f"Invalid package specification: {package}")
            else:
                self.logger.error("Pip information is not a list.")
        if "env_variables" in environment_info:
            env_vars = environment_info["env_variables"]
            if isinstance(env_vars, dict):
                for key, value in env_vars.items():
                    os.environ[key] = value
                    self.logger.info(f"Set environment variable {key} to {value}")
            else:
                self.logger.error(
                    "Environment variables information is not a dictionary."
                )
        # if working directory is specified download the data (TODO: This isn't supported yet)
        if "mount" in environment_info:
            mount = environment_info["mount"]
            if isinstance(mount, list):
                for path in mount:
                    if os.path.exists(path):
                        self.logger.info(f"Mounting {path}...")
                    else:
                        self.logger.error(f"Path {path} does not exist.")
            else:
                self.logger.error("Mount information is not a list.")
