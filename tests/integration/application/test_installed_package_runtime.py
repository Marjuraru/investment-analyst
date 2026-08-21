"""Hermetic smoke test for the package installed from the candidate wheel."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


_PROBE = r"""
import sys
from importlib import resources
from pathlib import Path

site_root = Path(sys.argv[1]).resolve()
source_root = Path(sys.argv[2]).resolve() / "src"
workspace_root = Path(sys.argv[3]).resolve()
sys.path.insert(0, str(site_root))

import investment_analyst

package_path = Path(investment_analyst.__file__ or "").resolve()
assert package_path.is_relative_to(site_root)
assert not package_path.is_relative_to(source_root)


def resource_exists(package_name, resource_name):
    resource = resources.files(package_name)
    for part in resource_name.split("/"):
        resource = resource.joinpath(part)
    return resource.is_file()


required_resources = (
    ("investment_analyst.catalog", "default_assets.v1.json"),
    ("investment_analyst.storage.migrations", "001_initial.sql"),
    ("investment_analyst.frontend", "static/index.html"),
    ("investment_analyst.frontend", "static/app.js"),
    ("investment_analyst.frontend", "static/styles.css"),
)
assert all(resource_exists(*resource) for resource in required_resources)

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.workspace.models import WorkspaceAccessMode

catalog = AssetCatalogService.load_default()
assert catalog.catalog_version == 1
runtime = ApplicationRuntime.create_default()
initialization = runtime.workspace_service.initialize(workspace_root)
assert initialization.storage_initialized
with runtime.open_storage(
    StorageLocationRequest(workspace=workspace_root),
    access_mode=WorkspaceAccessMode.READ_WRITE,
) as storage:
    assert storage.is_open
"""


def test_installed_wheel_contains_runtime_resources_and_initializes_storage(
    tmp_path: Path,
) -> None:
    """Build and import the wheel outside the checkout with no provider access."""
    wheel_dir = tmp_path / "wheel"
    site_packages = tmp_path / "site-packages"
    external_cwd = tmp_path / "external-cwd"
    wheel_dir.mkdir()
    site_packages.mkdir()
    external_cwd.mkdir()

    environment = os.environ.copy()
    for variable in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "SEC_USER_AGENT",
        "COVERAGE_PROCESS_START",
        "COVERAGE_PROCESS_CONFIG",
        "COVERAGE_FILE",
        "COVERAGE_RCFILE",
        "COVERAGE_RUN",
    ):
        environment.pop(variable, None)
    environment["PYTHONNOUSERSITE"] = "1"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            ".",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = tuple(wheel_dir.glob("investment_analyst-*.whl"))
    assert len(wheels) == 1

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(site_packages),
            str(wheels[0]),
        ],
        cwd=external_cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    workspace = tmp_path / "workspace"
    subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            str(site_packages),
            str(ROOT),
            str(workspace),
        ],
        cwd=external_cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
