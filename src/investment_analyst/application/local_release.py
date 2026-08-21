"""Independent local release runtime management for production deployments."""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import ConfigDict, field_validator

from investment_analyst.application.local_service_unit import write_local_service_unit
from investment_analyst.application.operational_state import (
    AaplDailyRunLock,
    AaplOperationalStateError,
)
from investment_analyst.core.models.base import ContractModel, UTCDateTime

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
PYTHON_312_PATTERN = re.compile(r"^Python 3\.12(?:\.\d+)?")
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_ENVIRONMENT_VARS = frozenset({"ALPACA_API_KEY", "ALPACA_API_SECRET", "SEC_USER_AGENT"})

DEFAULT_RUNTIME_ROOT = Path("~/.local/share/investment-analyst/runtime")
DEFAULT_SERVICE_ENV_PATH = Path("~/.config/investment-analyst/service.env")
DEFAULT_SYSTEMD_UNIT_PATH = Path("~/.config/systemd/user/investment-analyst.service")
DEFAULT_ORIGIN_URL = "https://github.com/Marjuraru/investment-analyst.git"
REQUIRED_UV_VERSION = "0.11.29"
LOCAL_SERVICE_UNIT_NAME = "investment-analyst.service"
DEFAULT_READINESS_DEADLINE: float = 120.0
MIN_READINESS_DEADLINE: float = 1.0
MAX_READINESS_DEADLINE: float = 600.0
DEFAULT_READINESS_INTERVAL: float = 0.25


class LocalReleaseError(Exception):
    """Base exception for all local release runtime operations."""


class ReleaseAcquisitionError(LocalReleaseError):
    """Raised when release acquisition, validation, or extraction fails."""


class ReleaseEnvironmentError(LocalReleaseError):
    """Raised when release virtual environment build fails."""


class ReleaseConfigurationError(LocalReleaseError):
    """Raised when private environment adoption or validation fails."""


class ReleaseUnitError(LocalReleaseError):
    """Raised when systemd service unit parsing or retargeting fails."""


class ReleaseVerificationError(LocalReleaseError):
    """Raised when pre-restart or health verification fails."""


class ReleaseRollbackError(LocalReleaseError):
    """Raised when rollback operations or automatic recovery fail."""


class ReleaseManifest(ContractModel):
    """Auditable, deterministic manifest for one materialized release."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "local-release-manifest-v1"
    commit_sha: str
    tree_sha: str
    uv_lock_sha256: str
    uv_version: str = REQUIRED_UV_VERSION
    python_version: str
    staged_at: UTCDateTime
    release_path: str

    @field_validator("commit_sha", "tree_sha")
    @classmethod
    def validate_full_sha(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not FULL_SHA.fullmatch(cleaned):
            raise ValueError("commit and tree SHAs must be 40 hexadecimal characters")
        return cleaned

    @field_validator("uv_lock_sha256")
    @classmethod
    def validate_lock_hash(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not HEX_64.fullmatch(cleaned):
            raise ValueError("uv_lock_sha256 must be a 64 hexadecimal character hash")
        return cleaned


class DeploymentState(ContractModel):
    """Persistent state tracking the active and previous release deployments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "local-deployment-state-v1"
    current: str | None = None
    previous: str | None = None
    updated_at: UTCDateTime
    current_release_path: str | None = None
    previous_release_path: str | None = None

    @field_validator("current", "previous")
    @classmethod
    def validate_deployment_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not FULL_SHA.fullmatch(cleaned):
            raise ValueError("deployment SHA must be 40 hexadecimal characters")
        return cleaned


class ReleaseStatusReport(ContractModel):
    """Comprehensive status report of the local release runtime and unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "local-release-status-report-v1"
    current_commit: str | None = None
    previous_commit: str | None = None
    current_release_dir: str | None = None
    current_manifest: ReleaseManifest | None = None
    unit_path: str
    unit_working_directory: str | None = None
    unit_exec_start: str | None = None
    unit_environment_file: str | None = None
    unit_matches_current: bool = False
    service_active: bool | None = None
    service_enabled: bool | None = None
    overview_status: int | None = None


@dataclass(frozen=True, slots=True)
class LocalReleasePaths:
    """Resolved directory layout for the independent local release runtime."""

    root: Path
    mirror: Path
    releases: Path
    tools: Path
    state_file: Path

    @classmethod
    def from_root(cls, root: Path | str | None = None) -> LocalReleasePaths:
        resolved_root = (
            Path(root).expanduser().resolve(strict=False)
            if root is not None
            else DEFAULT_RUNTIME_ROOT.expanduser().resolve(strict=False)
        )
        return cls(
            root=resolved_root,
            mirror=resolved_root / "mirror.git",
            releases=resolved_root / "releases",
            tools=resolved_root / "tools",
            state_file=resolved_root / "deployment_state.json",
        )


@runtime_checkable
class SystemctlRunner(Protocol):
    """Protocol for systemd control operations."""

    def daemon_reload(self) -> None: ...
    def restart(self, service: str) -> None: ...
    def stop(self, service: str) -> None: ...
    def is_active(self, service: str) -> bool: ...
    def is_enabled(self, service: str) -> bool: ...
    def show_properties(self, service: str, properties: Sequence[str]) -> Mapping[str, str]: ...


class RealSystemctlRunner:
    """Standard implementation executing systemctl commands on user session."""

    def daemon_reload(self) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseUnitError(f"systemctl daemon-reload failed: {error}") from error

    def restart(self, service: str) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", service],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseUnitError(f"systemctl restart {service} failed: {error}") from error

    def stop(self, service: str) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", service],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseUnitError(f"systemctl stop {service} failed: {error}") from error

    def is_active(self, service: str) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0 and result.stdout.strip() == "active"
        except (subprocess.SubprocessError, OSError):
            return False

    def is_enabled(self, service: str) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", service],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0 and result.stdout.strip() == "enabled"
        except (subprocess.SubprocessError, OSError):
            return False

    def show_properties(self, service: str, properties: Sequence[str]) -> Mapping[str, str]:
        arguments = ["systemctl", "--user", "show", service]
        for prop in properties:
            arguments.extend(["-p", prop])
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            properties_map: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    properties_map[key.strip()] = value.strip()
            return properties_map
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseUnitError(f"systemctl show failed: {error}") from error


class SimulatedSystemctlRunner:
    """In-memory simulated systemctl runner for hermetic testing and dry runs."""

    def __init__(
        self,
        is_active_result: bool = True,
        is_enabled_result: bool = True,
        properties: Mapping[str, str] | None = None,
        fail_restart: bool = False,
        fail_show: bool = False,
    ) -> None:
        self.is_active_result = is_active_result
        self.is_enabled_result = is_enabled_result
        self.properties = dict(properties or {})
        self.fail_restart = fail_restart
        self.fail_show = fail_show
        self.reloaded = False
        self.restarts: list[str] = []
        self.stops: list[str] = []

    def daemon_reload(self) -> None:
        self.reloaded = True

    def restart(self, service: str) -> None:
        if self.fail_restart:
            raise ReleaseUnitError(f"Simulated restart failure for {service}")
        self.restarts.append(service)

    def stop(self, service: str) -> None:
        self.stops.append(service)
        self.is_active_result = False

    def is_active(self, service: str) -> bool:
        return self.is_active_result

    def is_enabled(self, service: str) -> bool:
        return self.is_enabled_result

    def show_properties(self, service: str, properties: Sequence[str]) -> Mapping[str, str]:
        if self.fail_show:
            raise ReleaseUnitError("Simulated systemctl show failure")
        return {k: self.properties.get(k, "") for k in properties}


@runtime_checkable
class HealthChecker(Protocol):
    """Protocol for checking service HTTP health."""

    def check_health(
        self,
        port: int,
        endpoints: Sequence[str] = ("/api/v1/overview",),
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, int | None]: ...

    def check_health_readiness(
        self,
        port: int,
        endpoints: Sequence[str] = ("/api/v1/overview",),
        deadline_seconds: float = DEFAULT_READINESS_DEADLINE,
        interval_seconds: float = 0.25,
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, int | None]: ...


class RealHealthChecker:
    """Standard HTTP health checker using urllib to query loopback endpoints."""

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self._sleeper = sleeper if sleeper is not None else time.sleep

    def check_health(
        self,
        port: int,
        endpoints: Sequence[str] = ("/api/v1/overview",),
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, int | None]:
        last_status: int | None = None
        started_at = self._clock()
        for endpoint in endpoints:
            remaining = timeout_seconds - (self._clock() - started_at)
            if remaining <= 0:
                return False, last_status
            url = f"http://127.0.0.1:{port}{endpoint}"
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Investment-Analyst-Deployer/1.0"},
                )
                with urllib.request.urlopen(request, timeout=remaining) as response:
                    last_status = response.status
                    if response.status != 200:
                        return False, response.status
            except (urllib.error.URLError, OSError, TimeoutError):
                return False, last_status
        return True, last_status

    def check_health_readiness(
        self,
        port: int,
        endpoints: Sequence[str] = ("/api/v1/overview",),
        deadline_seconds: float = DEFAULT_READINESS_DEADLINE,
        interval_seconds: float = 0.25,
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, int | None]:
        """Poll health endpoints until all respond 200 or deadline expires.

        Retries on ConnectionRefusedError, URLError, TimeoutError, and non-200
        status until all endpoints respond 200 or the deadline expires.
        A real timeout still fails closed.
        """
        start = self._clock()
        last_status: int | None = None
        while True:
            elapsed = self._clock() - start
            remaining = deadline_seconds - elapsed
            if remaining <= 0:
                return False, last_status
            ok, last_status = self.check_health(
                port=port,
                endpoints=endpoints,
                timeout_seconds=min(timeout_seconds, remaining),
            )
            if ok:
                return self._clock() - start <= deadline_seconds, last_status
            elapsed = self._clock() - start
            if elapsed >= deadline_seconds:
                return False, last_status
            remaining = deadline_seconds - elapsed
            self._sleeper(min(interval_seconds, remaining))
            if self._clock() - start >= deadline_seconds:
                return False, last_status


class SimulatedHealthChecker:
    """Simulated health checker for unit and integration testing.

    Supports probe-level sequences or callables via ``responses``, and
    deterministic time advance via injected ``clock`` and ``sleeper``.
    """

    def __init__(
        self,
        succeed: bool = True,
        status_code: int = 200,
        fail_endpoints: Sequence[str] = (),
        responses: Sequence[tuple[bool, int | None]]
        | Callable[[], tuple[bool, int | None]]
        | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.succeed = succeed
        self.status_code = status_code
        self.fail_endpoints = set(fail_endpoints)
        self._responses = list(responses) if isinstance(responses, Sequence) else responses
        self._response_index: int = 0
        self.call_count: int = 0
        self._clock = clock if clock is not None else time.monotonic
        self._sleeper = sleeper if sleeper is not None else time.sleep

    def check_health(
        self,
        port: int,
        endpoints: Sequence[str] = ("/api/v1/overview",),
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, int | None]:
        self.call_count += 1
        if callable(self._responses):
            return self._responses()
        if self._responses is not None and isinstance(self._responses, list):
            idx = min(self._response_index, len(self._responses) - 1)
            self._response_index += 1
            return self._responses[idx]
        for ep in endpoints:
            if ep in self.fail_endpoints:
                return False, 500
        return self.succeed, (self.status_code if self.succeed else 500)

    def check_health_readiness(
        self,
        port: int,
        endpoints: Sequence[str] = ("/api/v1/overview",),
        deadline_seconds: float = DEFAULT_READINESS_DEADLINE,
        interval_seconds: float = 0.25,
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, int | None]:
        """Poll check_health until success or deadline expires using clock/sleeper."""
        start = self._clock()
        last_status: int | None = None
        while True:
            elapsed = self._clock() - start
            remaining = deadline_seconds - elapsed
            if remaining <= 0:
                return False, last_status
            ok, last_status = self.check_health(
                port=port,
                endpoints=endpoints,
                timeout_seconds=min(timeout_seconds, remaining),
            )
            if ok:
                return self._clock() - start <= deadline_seconds, last_status
            elapsed = self._clock() - start
            if elapsed >= deadline_seconds:
                return False, last_status
            remaining = deadline_seconds - elapsed
            self._sleeper(min(interval_seconds, remaining))
            if self._clock() - start >= deadline_seconds:
                return False, last_status


def _safe_tar_extract(archive_bytes: bytes, destination: Path) -> None:
    """Extract tar archive preventing path traversal and unsafe member types."""
    with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:*") as tar:
        resolved_dest = destination.resolve(strict=False)
        for member in tar.getmembers():
            member_path = (destination / member.name).resolve(strict=False)
            if member_path != resolved_dest and resolved_dest not in member_path.parents:
                raise ReleaseAcquisitionError(
                    f"Unsafe path traversal attempt in archive member: {member.name}"
                )
            if member.isdev() or member.ischr() or member.isfifo():
                raise ReleaseAcquisitionError(f"Unsafe file type in archive member: {member.name}")
            if member.issym():
                link_target = (destination / member.name).parent / member.linkname
                if resolved_dest not in link_target.resolve(strict=False).parents:
                    raise ReleaseAcquisitionError(
                        f"Unsafe symlink in archive member: {member.name} -> {member.linkname}"
                    )
        tar.extractall(path=destination, filter="data")


def validate_environment_file_content(path: Path) -> None:
    """Require a private, well-formed environment file containing required variables."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise ReleaseConfigurationError(f"Could not inspect environment file: {error}") from error
    if mode & 0o077:
        raise ReleaseConfigurationError(
            "Environment file permissions must be 0600 or more restrictive"
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ReleaseConfigurationError(f"Environment file could not be read: {error}") from error

    present: set[str] = set()
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not ENVIRONMENT_NAME.fullmatch(name):
            raise ReleaseConfigurationError("Environment file must contain only NAME=value entries")
        if name in seen:
            raise ReleaseConfigurationError(f"Environment file contains duplicate variable: {name}")
        seen.add(name)
        if name in REQUIRED_ENVIRONMENT_VARS and value.strip() not in {"", "''", '""'}:
            present.add(name)
    if present != REQUIRED_ENVIRONMENT_VARS:
        missing = sorted(REQUIRED_ENVIRONMENT_VARS - present)
        raise ReleaseConfigurationError(
            f"Environment file is missing required non-empty variables: {', '.join(missing)}"
        )


_INSTALLED_RUNTIME_PROBE = r"""
import sys
from importlib import resources
from pathlib import Path
import sysconfig

release_root = Path(sys.argv[1]).resolve()
workspace_root = Path(sys.argv[2]).resolve()
site_root = Path(sysconfig.get_path("purelib")).resolve()

import investment_analyst

package_path = Path(investment_analyst.__file__ or "").resolve()
if site_root not in package_path.parents or "site-packages" not in package_path.parts:
    raise RuntimeError("investment_analyst was not imported from the installed site-packages")
if (release_root / "src") in package_path.parents:
    raise RuntimeError("investment_analyst was imported from the release source tree")

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
missing = [
    f"{package_name}/{resource_name}"
    for package_name, resource_name in required_resources
    if not resource_exists(package_name, resource_name)
]
if missing:
    raise RuntimeError("installed runtime resources are missing")

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.workspace.models import WorkspaceAccessMode

AssetCatalogService.load_default()
runtime = ApplicationRuntime.create_default()
initialization = runtime.workspace_service.initialize(workspace_root)
if not initialization.storage_initialized:
    raise RuntimeError("installed runtime workspace storage was not initialized")
with runtime.open_storage(
    StorageLocationRequest(workspace=workspace_root),
    access_mode=WorkspaceAccessMode.READ_WRITE,
) as storage:
    if not storage.is_open:
        raise RuntimeError("installed runtime storage did not open")
"""


class LocalReleaseService:
    """Core service for managing independent local releases and systemd units."""

    def __init__(
        self,
        paths: LocalReleasePaths | Path | None = None,
        repo_url: str = DEFAULT_ORIGIN_URL,
        systemctl: SystemctlRunner | None = None,
        health_checker: HealthChecker | None = None,
        systemd_unit_path: Path | None = None,
        service_env_path: Path | None = None,
        readiness_deadline: float = DEFAULT_READINESS_DEADLINE,
        readiness_interval: float = DEFAULT_READINESS_INTERVAL,
    ) -> None:
        if (
            not math.isfinite(readiness_deadline)
            or readiness_deadline < MIN_READINESS_DEADLINE
            or readiness_deadline > MAX_READINESS_DEADLINE
        ):
            raise ReleaseConfigurationError("Readiness deadline must be between 1 and 600 seconds")
        if isinstance(paths, LocalReleasePaths):
            self.paths = paths
        else:
            self.paths = LocalReleasePaths.from_root(paths)
        self.repo_url = repo_url
        self.systemctl = systemctl if systemctl is not None else RealSystemctlRunner()
        self.health_checker = health_checker if health_checker is not None else RealHealthChecker()
        self.systemd_unit_path = (
            systemd_unit_path.expanduser().resolve(strict=False)
            if systemd_unit_path is not None
            else DEFAULT_SYSTEMD_UNIT_PATH.expanduser().resolve(strict=False)
        )
        self.service_env_path = (
            service_env_path.expanduser().resolve(strict=False)
            if service_env_path is not None
            else DEFAULT_SERVICE_ENV_PATH.expanduser().resolve(strict=False)
        )
        self.readiness_deadline = readiness_deadline
        self.readiness_interval = readiness_interval

    def _validate_installed_runtime(self, python_bin: Path, release_root: Path) -> None:
        """Validate the non-editable installed package and initialize temporary storage."""
        environment = os.environ.copy()
        for variable in (
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
            "ALPACA_API_KEY",
            "ALPACA_API_SECRET",
            "SEC_USER_AGENT",
        ):
            environment.pop(variable, None)
        environment["PYTHONNOUSERSITE"] = "1"
        with tempfile.TemporaryDirectory(prefix="investment-analyst-installed-probe-") as scratch:
            workspace_root = Path(scratch) / "workspace"
            try:
                subprocess.run(
                    [
                        str(python_bin),
                        "-c",
                        _INSTALLED_RUNTIME_PROBE,
                        str(release_root.resolve()),
                        str(workspace_root),
                    ],
                    cwd=scratch,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (subprocess.SubprocessError, OSError) as error:
                raise ReleaseAcquisitionError("Installed package runtime probe failed") from error

    def init_runtime(self) -> None:
        """Create runtime directories with strict 0700 permissions."""
        for directory in (
            self.paths.root,
            self.paths.releases,
            self.paths.tools,
            self.service_env_path.parent,
            self.systemd_unit_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                directory.chmod(0o700)

    def ensure_uv(self) -> Path:
        """Find or bootstrap uv 0.11.29 under the runtime root."""
        # 1. Check if uv in PATH matches REQUIRED_UV_VERSION
        system_uv = shutil.which("uv")
        if system_uv is not None:
            try:
                result = subprocess.run(
                    [system_uv, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and REQUIRED_UV_VERSION in result.stdout:
                    return Path(system_uv)
            except (subprocess.SubprocessError, OSError):
                pass

        # 2. Check runtime tool venv
        uv_tool_bin = self.paths.tools / "uv_env" / "bin" / "uv"
        if uv_tool_bin.is_file():
            try:
                result = subprocess.run(
                    [str(uv_tool_bin), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and REQUIRED_UV_VERSION in result.stdout:
                    return uv_tool_bin
            except (subprocess.SubprocessError, OSError):
                pass

        # 3. Bootstrap uv into runtime tools directory
        self.init_runtime()
        tool_venv = self.paths.tools / "uv_env"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(tool_venv)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            pip_bin = tool_venv / "bin" / "pip"
            subprocess.run(
                [str(pip_bin), "install", "-q", f"uv=={REQUIRED_UV_VERSION}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if not uv_tool_bin.is_file():
                raise ReleaseEnvironmentError("Bootstrap did not produce uv executable")
            return uv_tool_bin
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseEnvironmentError(
                f"Failed to bootstrap uv=={REQUIRED_UV_VERSION} in runtime: {error}"
            ) from error

    def _query_remote_main(self) -> str:
        """Query origin HTTPS repository for current refs/heads/main commit SHA."""
        try:
            ls_remote = subprocess.run(
                ["git", "ls-remote", self.repo_url, "refs/heads/main"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseAcquisitionError(
                f"Failed to query remote main at {self.repo_url}: {error}"
            ) from error

        lines = [line.strip() for line in ls_remote.stdout.splitlines() if line.strip()]
        if not lines:
            raise ReleaseAcquisitionError(
                f"No refs/heads/main found at remote repository {self.repo_url}"
            )
        remote_sha = lines[0].split()[0].lower()
        if not FULL_SHA.fullmatch(remote_sha):
            raise ReleaseAcquisitionError(
                f"Invalid remote SHA returned from ls-remote: {remote_sha}"
            )
        return remote_sha

    def fetch_origin_main(self, sha: str | None = None) -> tuple[str, str]:
        """Query origin/main, acquire into mirror, and reject moving ref during acquisition."""
        self.init_runtime()
        if sha is not None:
            cleaned_sha = sha.strip().lower()
            if not FULL_SHA.fullmatch(cleaned_sha):
                raise ReleaseAcquisitionError(
                    f"Requested SHA '{sha}' must be a full 40-character hexadecimal string"
                )
        else:
            cleaned_sha = None

        # 1. Initial remote query
        initial_remote_sha = self._query_remote_main()
        if cleaned_sha is not None and cleaned_sha != initial_remote_sha:
            raise ReleaseAcquisitionError(
                f"Requested SHA '{cleaned_sha}' does not match live origin/main "
                f"'{initial_remote_sha}'"
            )
        target_sha = cleaned_sha if cleaned_sha is not None else initial_remote_sha

        # 2. Mirror initialization & fetch
        if not self.paths.mirror.is_dir():
            try:
                subprocess.run(
                    ["git", "init", "--bare", str(self.paths.mirror)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (subprocess.SubprocessError, OSError) as error:
                raise ReleaseAcquisitionError(
                    f"Failed to initialize bare mirror at {self.paths.mirror}: {error}"
                ) from error

        try:
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(self.paths.mirror),
                    "fetch",
                    self.repo_url,
                    "+refs/heads/main:refs/heads/main",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseAcquisitionError(
                f"Failed to fetch refs/heads/main into mirror: {error}"
            ) from error

        # 3. Post-acquisition verification: reject moving origin/main
        post_remote_sha = self._query_remote_main()
        if post_remote_sha != target_sha:
            raise ReleaseAcquisitionError(
                f"Remote origin/main moved during acquisition: requested {target_sha}, "
                f"now {post_remote_sha}"
            )

        # 4. Verify commit object & tree in mirror
        try:
            commit_check = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(self.paths.mirror),
                    "rev-parse",
                    f"{target_sha}^{{commit}}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            verified_commit = commit_check.stdout.strip().lower()
            if verified_commit != target_sha:
                raise ReleaseAcquisitionError(
                    f"Mirror verified commit {verified_commit} does not match target {target_sha}"
                )
            tree_check = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(self.paths.mirror),
                    "rev-parse",
                    f"{target_sha}^{{tree}}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            verified_tree = tree_check.stdout.strip().lower()
            if not FULL_SHA.fullmatch(verified_tree):
                raise ReleaseAcquisitionError(f"Invalid tree SHA in mirror: {verified_tree}")
        except (subprocess.SubprocessError, OSError) as error:
            raise ReleaseAcquisitionError(
                f"Failed to verify commit object {target_sha} in mirror: {error}"
            ) from error

        return verified_commit, verified_tree

    def stage(self, sha: str) -> ReleaseManifest:
        """Materialize an immutable release for the exact SHA with locked virtualenv."""
        cleaned_sha = sha.strip().lower()
        if not FULL_SHA.fullmatch(cleaned_sha):
            raise ReleaseAcquisitionError(
                f"Commit SHA must be 40 lowercase hexadecimal characters, got: '{sha}'"
            )

        verified_commit, verified_tree = self.fetch_origin_main(cleaned_sha)
        release_target = self.paths.releases / verified_commit
        manifest_file = release_target / "manifest.json"

        # Check existing release for equivalence & immutability
        if release_target.is_dir():
            if not manifest_file.is_file():
                raise ReleaseAcquisitionError(
                    f"Preexisting release directory {release_target} is missing manifest.json"
                )
            try:
                existing_manifest = ReleaseManifest.model_validate_json(
                    manifest_file.read_text(encoding="utf-8")
                )
                python_bin = release_target / ".venv" / "bin" / "python"
                uv_lock_file = release_target / "uv.lock"
                pyproject_file = release_target / "pyproject.toml"
                serve_script = release_target / "scripts" / "serve_investment_analyst.py"

                if (
                    not python_bin.is_file()
                    or not uv_lock_file.is_file()
                    or not pyproject_file.is_file()
                    or not serve_script.is_file()
                ):
                    raise ReleaseAcquisitionError(
                        f"Preexisting release {release_target} is missing required runtime files"
                    )

                on_disk_lock_hash = hashlib.sha256(uv_lock_file.read_bytes()).hexdigest().lower()
                if (
                    existing_manifest.commit_sha != verified_commit
                    or existing_manifest.tree_sha != verified_tree
                    or existing_manifest.uv_lock_sha256 != on_disk_lock_hash
                    or existing_manifest.uv_version != REQUIRED_UV_VERSION
                    or not PYTHON_312_PATTERN.match(existing_manifest.python_version)
                ):
                    raise ReleaseAcquisitionError(
                        f"Preexisting release at {release_target} is not "
                        f"equivalent to {verified_commit}"
                    )

                # Verify python execution & package import
                py_check = subprocess.run(
                    [str(python_bin), "--version"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if not PYTHON_312_PATTERN.match(py_check.stdout.strip()):
                    raise ReleaseAcquisitionError(
                        f"Preexisting release python is not Python 3.12: {py_check.stdout.strip()}"
                    )
                subprocess.run(
                    [str(python_bin), "-c", "import investment_analyst"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self._validate_installed_runtime(python_bin, release_target)
                return existing_manifest
            except Exception as error:
                if isinstance(error, ReleaseAcquisitionError):
                    raise
                raise ReleaseAcquisitionError(
                    f"Preexisting release at {release_target} has corrupt "
                    f"manifest or state: {error}"
                ) from error

        staging_dir = self.paths.releases / f".staging-{verified_commit}-{uuid4().hex}"
        staging_dir.mkdir(parents=True, exist_ok=False)
        try:
            archive_proc = subprocess.run(
                ["git", "--git-dir", str(self.paths.mirror), "archive", verified_commit],
                check=True,
                capture_output=True,
                timeout=60,
            )
            _safe_tar_extract(archive_proc.stdout, staging_dir)

            pyproject_file = staging_dir / "pyproject.toml"
            uv_lock_file = staging_dir / "uv.lock"
            serve_script = staging_dir / "scripts" / "serve_investment_analyst.py"
            if (
                not pyproject_file.is_file()
                or not uv_lock_file.is_file()
                or not serve_script.is_file()
            ):
                raise ReleaseAcquisitionError(
                    "Materialized tree is missing pyproject.toml, uv.lock, or serve script"
                )

            uv_lock_hash = hashlib.sha256(uv_lock_file.read_bytes()).hexdigest().lower()
            uv_bin = self.ensure_uv()
            python_path = sys.executable

            subprocess.run(
                [
                    str(uv_bin),
                    "venv",
                    str(staging_dir / ".venv"),
                    "--python",
                    python_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )

            subprocess.run(
                [
                    str(uv_bin),
                    "sync",
                    "--locked",
                    "--no-dev",
                    "--no-editable",
                    "--project",
                    str(staging_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )

            staged_python = staging_dir / ".venv" / "bin" / "python"
            if not staged_python.is_file():
                raise ReleaseEnvironmentError("Virtualenv python binary was not created")

            py_version_check = subprocess.run(
                [str(staged_python), "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            python_version = py_version_check.stdout.strip()
            if not PYTHON_312_PATTERN.match(python_version):
                raise ReleaseEnvironmentError(
                    f"Release virtual environment must use Python 3.12, found: '{python_version}'"
                )

            subprocess.run(
                [str(staged_python), "-c", "import investment_analyst"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self._validate_installed_runtime(staged_python, staging_dir)

            manifest = ReleaseManifest(
                schema_version="local-release-manifest-v1",
                commit_sha=verified_commit,
                tree_sha=verified_tree,
                uv_lock_sha256=uv_lock_hash,
                uv_version=REQUIRED_UV_VERSION,
                python_version=python_version,
                staged_at=datetime.now(UTC),
                release_path=str(release_target),
            )
            (staging_dir / "manifest.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )

            staging_dir.rename(release_target)
            return manifest
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def adopt_env(self, source: Path, destination: Path | None = None) -> Path:
        """Privately adopt an environment file with 0600 permissions without leaking values."""
        resolved_source = source.expanduser().resolve(strict=False)
        if not resolved_source.is_file():
            raise ReleaseConfigurationError(
                f"Source environment file does not exist: {resolved_source}"
            )
        validate_environment_file_content(resolved_source)

        target_dest = (
            destination.expanduser().resolve(strict=False)
            if destination is not None
            else self.service_env_path
        )

        source_bytes = resolved_source.read_bytes()
        if target_dest.is_file():
            dest_bytes = target_dest.read_bytes()
            if dest_bytes == source_bytes:
                return target_dest
            raise ReleaseConfigurationError(
                f"Destination environment file already exists with different content: {target_dest}"
            )

        target_dest.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            target_dest.parent.chmod(0o700)

        temp_dest = target_dest.with_name(f".{target_dest.name}.{uuid4().hex}.tmp")
        fd: int | None = None
        try:
            fd = os.open(temp_dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with open(fd, "wb", closefd=True) as stream:
                fd = None
                stream.write(source_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_dest, target_dest)
        finally:
            if fd is not None:
                os.close(fd)
            temp_dest.unlink(missing_ok=True)

        return target_dest

    def retarget_unit(
        self,
        sha: str,
        unit_file: Path | None = None,
        env_file: Path | None = None,
    ) -> str:
        """Atomically retarget systemd user unit to an independent staged release."""
        cleaned_sha = sha.strip().lower()
        if not FULL_SHA.fullmatch(cleaned_sha):
            raise ReleaseUnitError(f"Invalid release SHA for retarget: '{sha}'")

        release_dir = self.paths.releases / cleaned_sha
        if not release_dir.is_dir():
            raise ReleaseUnitError(f"Release directory does not exist: {release_dir}")

        new_python = release_dir / ".venv" / "bin" / "python"
        new_script = release_dir / "scripts" / "serve_investment_analyst.py"
        if not new_python.is_file() or not new_script.is_file():
            raise ReleaseUnitError(
                f"Release directory {release_dir} is missing python or server script"
            )

        target_unit = (
            unit_file.expanduser().resolve(strict=False)
            if unit_file is not None
            else self.systemd_unit_path
        )
        if not target_unit.is_file():
            raise ReleaseUnitError(f"Existing unit file not found at: {target_unit}")

        target_env = (
            env_file.expanduser().resolve(strict=False)
            if env_file is not None
            else self.service_env_path
        )
        if not target_env.is_file():
            raise ReleaseUnitError(f"Environment file not found at: {target_env}")

        try:
            content = target_unit.read_text(encoding="utf-8")
        except OSError as error:
            raise ReleaseUnitError(f"Could not read unit file {target_unit}: {error}") from error

        lines = content.splitlines()
        found_service_section = False
        working_dir_idx: int | None = None
        env_file_idx: int | None = None
        exec_start_idx: int | None = None

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "[Service]":
                found_service_section = True
            elif found_service_section and stripped.startswith("["):
                break
            elif found_service_section:
                if stripped.startswith("WorkingDirectory="):
                    if working_dir_idx is not None:
                        raise ReleaseUnitError("Duplicate WorkingDirectory in unit file")
                    working_dir_idx = idx
                elif stripped.startswith("EnvironmentFile="):
                    if env_file_idx is not None:
                        raise ReleaseUnitError("Duplicate EnvironmentFile in unit file")
                    env_file_idx = idx
                elif stripped.startswith("ExecStart="):
                    if exec_start_idx is not None:
                        raise ReleaseUnitError("Duplicate ExecStart in unit file")
                    exec_start_idx = idx

        if (
            not found_service_section
            or working_dir_idx is None
            or env_file_idx is None
            or exec_start_idx is None
        ):
            raise ReleaseUnitError(
                "Unit file is missing [Service], WorkingDirectory, EnvironmentFile, or ExecStart"
            )

        existing_exec_start = lines[exec_start_idx].split("=", maxsplit=1)[1].strip()
        match = re.search(r"serve_investment_analyst\.py(?:\s*\"|\s+)(.*)$", existing_exec_start)
        if not match:
            raise ReleaseUnitError(f"ExecStart invocation unrecognized: {existing_exec_start}")
        trailing_args = match.group(1).strip()

        new_lines = list(lines)
        new_lines[working_dir_idx] = f"WorkingDirectory={str(release_dir)}"
        new_lines[env_file_idx] = f"EnvironmentFile={str(target_env)}"
        new_lines[exec_start_idx] = (
            f'ExecStart="{str(new_python)}" "{str(new_script)}" {trailing_args}'.rstrip()
        )

        updated_document = "\n".join(new_lines)
        if not updated_document.endswith("\n"):
            updated_document += "\n"

        write_local_service_unit(target_unit, updated_document)
        return updated_document

    def verify_pre_restart(
        self,
        sha: str,
        unit_file: Path,
        workspace_root: Path | None = None,
        port: int = 8765,
        skip_systemd: bool = False,
    ) -> None:
        """Verify staged release, valid unit, port, workspace, and absence of active writer."""
        cleaned_sha = sha.strip().lower()
        if not FULL_SHA.fullmatch(cleaned_sha):
            raise ReleaseVerificationError(f"Invalid release SHA: '{sha}'")

        # 1. Verify staged release files
        release_target = self.paths.releases / cleaned_sha
        manifest_file = release_target / "manifest.json"
        python_bin = release_target / ".venv" / "bin" / "python"
        serve_script = release_target / "scripts" / "serve_investment_analyst.py"
        if (
            not release_target.is_dir()
            or not manifest_file.is_file()
            or not python_bin.is_file()
            or not serve_script.is_file()
        ):
            raise ReleaseVerificationError(
                f"Release {cleaned_sha} is not fully staged at {release_target}"
            )

        # 2. Verify unit file
        if not unit_file.is_file():
            raise ReleaseVerificationError(f"Unit file not found at: {unit_file}")
        try:
            unit_content = unit_file.read_text(encoding="utf-8")
        except OSError as error:
            raise ReleaseVerificationError(
                f"Could not read unit file {unit_file}: {error}"
            ) from error

        if "[Service]" not in unit_content or "ExecStart=" not in unit_content:
            raise ReleaseVerificationError("Unit file is missing [Service] or ExecStart")

        # 3. Verify port range
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ReleaseVerificationError(
                f"Port must be an integer between 1 and 65535, got {port}"
            )

        # 4. Verify workspace and writer locks via canonical AaplDailyRunLock.is_held
        resolved_ws: Path | None = None
        if workspace_root is not None:
            resolved_ws = workspace_root.expanduser().resolve(strict=False)
        else:
            # Extract --workspace from ExecStart in unit_file
            ws_match = re.search(r'--workspace(?:\s+"([^"]+)"|\s+([^\s]+))', unit_content)
            if ws_match:
                extracted = ws_match.group(1) or ws_match.group(2)
                resolved_ws = Path(extracted).expanduser().resolve(strict=False)

        if resolved_ws is not None:
            if not resolved_ws.is_dir():
                raise ReleaseVerificationError(f"Workspace directory not found: {resolved_ws}")
            daily_lock = resolved_ws / "state" / "aapl_daily_run.lock"
            try:
                is_held = AaplDailyRunLock.is_held(daily_lock)
            except AaplOperationalStateError as error:
                raise ReleaseVerificationError(
                    f"Could not inspect workspace writer lock {daily_lock}: {error}"
                ) from error

            if is_held:
                raise ReleaseVerificationError(
                    f"Active writer operation detected in workspace: {daily_lock}"
                )

        # 5. Verify systemctl if not skipped
        if not skip_systemd:
            try:
                self.systemctl.show_properties(LOCAL_SERVICE_UNIT_NAME, ("Id",))
            except Exception as error:
                raise ReleaseVerificationError(
                    f"Current systemd service verification failed: {error}"
                ) from error

    def load_deployment_state(self) -> DeploymentState:
        """Load persistent deployment state or return default empty state."""
        if not self.paths.state_file.is_file():
            return DeploymentState(
                schema_version="local-deployment-state-v1",
                current=None,
                previous=None,
                updated_at=datetime.now(UTC),
                current_release_path=None,
                previous_release_path=None,
            )
        try:
            return DeploymentState.model_validate_json(
                self.paths.state_file.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise ReleaseVerificationError(
                f"Deployment state file at {self.paths.state_file} is corrupt: {error}"
            ) from error

    def _save_deployment_state(self, state: DeploymentState) -> None:
        """Persist deployment state atomically."""
        self.init_runtime()
        temp_state = self.paths.state_file.with_name(
            f".{self.paths.state_file.name}.{uuid4().hex}.tmp"
        )
        temp_state.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temp_state.replace(self.paths.state_file)

    def _validate_current_deployment(
        self,
        state: DeploymentState,
        unit_file: Path,
    ) -> str:
        """Validate the managed current release or classify the unit as unmanaged legacy."""
        if state.current is None:
            if state.previous is not None:
                raise ReleaseRollbackError(
                    "CRITICAL: deployment state has previous without a managed current release"
                )
            try:
                return unit_file.read_text(encoding="utf-8")
            except OSError as error:
                raise ReleaseVerificationError(
                    f"Could not read existing legacy unit {unit_file}: {error}"
                ) from error

        current_release = self.paths.releases / state.current
        manifest_file = current_release / "manifest.json"
        try:
            manifest = ReleaseManifest.model_validate_json(
                manifest_file.read_text(encoding="utf-8")
            )
            unit_text = unit_file.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise ReleaseRollbackError(
                f"CRITICAL: managed current release {state.current} is not verifiable"
            ) from error

        current_python = current_release / ".venv" / "bin" / "python"
        current_script = current_release / "scripts" / "serve_investment_analyst.py"
        working_directory = next(
            (
                line.strip().split("=", maxsplit=1)[1]
                for line in unit_text.splitlines()
                if line.strip().startswith("WorkingDirectory=")
            ),
            None,
        )
        exec_start = next(
            (
                line.strip()
                for line in unit_text.splitlines()
                if line.strip().startswith("ExecStart=")
            ),
            None,
        )

        if (
            manifest.commit_sha != state.current
            or Path(manifest.release_path).resolve() != current_release.resolve()
            or state.current_release_path != str(current_release)
            or not current_python.is_file()
            or not current_script.is_file()
            or working_directory != str(current_release)
            or exec_start is None
            or f'ExecStart="{current_python}" "{current_script}"' not in exec_start
        ):
            raise ReleaseRollbackError(
                f"CRITICAL: deployment state, manifest, and live unit disagree for current "
                f"release {state.current}"
            )
        return unit_text

    def _fail_first_adoption_safely(
        self,
        *,
        sha: str,
        unit_file: Path,
        previous_unit_text: str,
        failure: BaseException,
    ) -> None:
        """Stop a failed first-adoption candidate without restarting unmanaged legacy."""
        cleanup_errors: list[str] = []
        try:
            self.systemctl.stop(LOCAL_SERVICE_UNIT_NAME)
        except Exception as error:
            cleanup_errors.append(f"candidate stop failed: {error}")

        restored = False
        try:
            write_local_service_unit(unit_file, previous_unit_text)
            restored = True
        except Exception as error:
            cleanup_errors.append(f"legacy unit restoration failed: {error}")

        if restored:
            try:
                self.systemctl.daemon_reload()
            except Exception as error:
                cleanup_errors.append(f"daemon-reload failed: {error}")

        try:
            if self.systemctl.is_active(LOCAL_SERVICE_UNIT_NAME):
                cleanup_errors.append("service remained active after candidate stop")
        except Exception as error:
            cleanup_errors.append(f"service inactivity could not be confirmed: {error}")

        if cleanup_errors:
            details = "; ".join(cleanup_errors)
            raise ReleaseRollbackError(
                f"CRITICAL: first adoption of {sha} failed: {failure}; "
                f"original unit is unmanaged legacy and recovery was not confirmed "
                f"(ALSO FAILED): {details}"
            ) from failure
        raise ReleaseRollbackError(
            f"First adoption of {sha} failed: {failure}; candidate stopped, "
            "unmanaged legacy unit restored but not restarted, service inactive confirmed"
        ) from failure

    def _recover_managed_current(
        self,
        *,
        current_sha: str,
        unit_file: Path,
        previous_unit_text: str,
        port: int,
        health_endpoints: Sequence[str],
        failure_message: str,
        failure: BaseException,
    ) -> None:
        """Restore and verify a previously managed current release after a failed activation."""
        try:
            write_local_service_unit(unit_file, previous_unit_text)
            self.systemctl.daemon_reload()
            self.systemctl.restart(LOCAL_SERVICE_UNIT_NAME)
            previous_ok, previous_status = self.health_checker.check_health_readiness(
                port=port,
                endpoints=health_endpoints,
                deadline_seconds=self.readiness_deadline,
                interval_seconds=self.readiness_interval,
            )
        except Exception as error:
            raise ReleaseRollbackError(
                f"CRITICAL: {failure_message}; recovery rollback to managed current "
                f"{current_sha} ALSO FAILED: {error}"
            ) from failure
        if previous_ok:
            raise ReleaseRollbackError(
                f"{failure_message}; successfully rolled back and verified managed current "
                f"{current_sha} (health status {previous_status})"
            ) from failure
        raise ReleaseRollbackError(
            f"CRITICAL: {failure_message}; recovery rollback to managed current "
            f"{current_sha} ALSO FAILED health verification (status {previous_status})"
        ) from failure

    def activate(
        self,
        sha: str,
        unit_file: Path | None = None,
        env_file: Path | None = None,
        workspace_root: Path | None = None,
        port: int = 8765,
        skip_systemd: bool = False,
        skip_health_check: bool = False,
        endpoints: Sequence[str] | None = None,
    ) -> DeploymentState:
        """Activate release with pre-restart verification, health check, and verified rollback."""
        cleaned_sha = sha.strip().lower()
        if not FULL_SHA.fullmatch(cleaned_sha):
            raise ReleaseAcquisitionError(f"Invalid activation SHA: '{sha}'")

        release_target = self.paths.releases / cleaned_sha
        target_unit = (
            unit_file.expanduser().resolve(strict=False)
            if unit_file is not None
            else self.systemd_unit_path
        )
        target_env = (
            env_file.expanduser().resolve(strict=False)
            if env_file is not None
            else self.service_env_path
        )

        # 1. Pre-restart verification (fails closed)
        self.verify_pre_restart(
            sha=cleaned_sha,
            unit_file=target_unit,
            workspace_root=workspace_root,
            port=port,
            skip_systemd=skip_systemd,
        )

        current_state = self.load_deployment_state()
        previous_unit_text = self._validate_current_deployment(current_state, target_unit)
        managed_current = current_state.current is not None

        # Idempotence check
        if current_state.current == cleaned_sha:
            if skip_health_check or skip_systemd:
                return current_state
            health_ok, _ = self.health_checker.check_health(
                port=port,
                endpoints=endpoints or ("/api/v1/overview", "/api/v1/candidate-notifications"),
            )
            if health_ok:
                return current_state

        # 2. Retarget unit
        self.retarget_unit(cleaned_sha, unit_file=target_unit, env_file=target_env)

        # 3. Systemd restart and health check with readiness polling
        if not skip_systemd:
            health_endpoints = endpoints or (
                "/api/v1/overview",
                "/api/v1/candidate-notifications",
            )
            try:
                self.systemctl.daemon_reload()
                self.systemctl.restart(LOCAL_SERVICE_UNIT_NAME)
            except Exception as error:
                failure_message = f"Systemd restart failed for {cleaned_sha}: {error}"
                if managed_current:
                    self._recover_managed_current(
                        current_sha=current_state.current or "",
                        unit_file=target_unit,
                        previous_unit_text=previous_unit_text,
                        port=port,
                        health_endpoints=health_endpoints,
                        failure_message=failure_message,
                        failure=error,
                    )
                self._fail_first_adoption_safely(
                    sha=cleaned_sha,
                    unit_file=target_unit,
                    previous_unit_text=previous_unit_text,
                    failure=error,
                )

            if not skip_health_check:
                health_ok, status_code = self.health_checker.check_health_readiness(
                    port=port,
                    endpoints=health_endpoints,
                    deadline_seconds=self.readiness_deadline,
                    interval_seconds=self.readiness_interval,
                )
                if not health_ok:
                    failure_message = (
                        f"Health check failed for {cleaned_sha} with status {status_code}"
                    )
                    if managed_current:
                        self._recover_managed_current(
                            current_sha=current_state.current or "",
                            unit_file=target_unit,
                            previous_unit_text=previous_unit_text,
                            port=port,
                            health_endpoints=health_endpoints,
                            failure_message=failure_message,
                            failure=ReleaseVerificationError(failure_message),
                        )
                    self._fail_first_adoption_safely(
                        sha=cleaned_sha,
                        unit_file=target_unit,
                        previous_unit_text=previous_unit_text,
                        failure=ReleaseVerificationError(failure_message),
                    )

        new_state = DeploymentState(
            schema_version="local-deployment-state-v1",
            current=cleaned_sha,
            previous=current_state.current,
            updated_at=datetime.now(UTC),
            current_release_path=str(release_target),
            previous_release_path=current_state.current_release_path,
        )
        self._save_deployment_state(new_state)
        return new_state

    def update(
        self,
        sha: str,
        unit_file: Path | None = None,
        env_file: Path | None = None,
        workspace_root: Path | None = None,
        port: int = 8765,
        skip_systemd: bool = False,
        skip_health_check: bool = False,
        endpoints: Sequence[str] | None = None,
    ) -> DeploymentState:
        """High-level deployment command: stages the SHA and activates it."""
        self.stage(sha)
        return self.activate(
            sha=sha,
            unit_file=unit_file,
            env_file=env_file,
            workspace_root=workspace_root,
            port=port,
            skip_systemd=skip_systemd,
            skip_health_check=skip_health_check,
            endpoints=endpoints,
        )

    def rollback(
        self,
        unit_file: Path | None = None,
        env_file: Path | None = None,
        port: int = 8765,
        skip_systemd: bool = False,
        skip_health_check: bool = False,
    ) -> DeploymentState:
        """Rollback to the verified previous deployment with health check."""
        current_state = self.load_deployment_state()
        if current_state.previous is None:
            raise ReleaseRollbackError("No previous deployment recorded for rollback")

        target_unit = (
            unit_file.expanduser().resolve(strict=False)
            if unit_file is not None
            else self.systemd_unit_path
        )
        previous_unit_text = self._validate_current_deployment(current_state, target_unit)
        if current_state.current is None:
            raise ReleaseRollbackError("CRITICAL: rollback requires a managed current release")

        prev_sha = current_state.previous
        prev_release_dir = self.paths.releases / prev_sha
        prev_manifest = prev_release_dir / "manifest.json"
        try:
            previous_manifest = ReleaseManifest.model_validate_json(
                prev_manifest.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise ReleaseRollbackError(
                f"Previous release {prev_sha} is missing manifest at {prev_manifest}"
            ) from error
        if (
            previous_manifest.commit_sha != prev_sha
            or Path(previous_manifest.release_path).resolve() != prev_release_dir.resolve()
            or not (prev_release_dir / ".venv" / "bin" / "python").is_file()
            or not (prev_release_dir / "scripts" / "serve_investment_analyst.py").is_file()
        ):
            raise ReleaseRollbackError(
                f"CRITICAL: previous release {prev_sha} is not a verified rollback target"
            )

        self.retarget_unit(prev_sha, unit_file=target_unit, env_file=env_file)

        if not skip_systemd:
            try:
                self.systemctl.daemon_reload()
                self.systemctl.restart(LOCAL_SERVICE_UNIT_NAME)
            except Exception as error:
                self._recover_managed_current(
                    current_sha=current_state.current,
                    unit_file=target_unit,
                    previous_unit_text=previous_unit_text,
                    port=port,
                    health_endpoints=("/api/v1/overview", "/api/v1/candidate-notifications"),
                    failure_message=f"Restart failed during rollback to {prev_sha}: {error}",
                    failure=error,
                )

            if not skip_health_check:
                health_ok, status_code = self.health_checker.check_health_readiness(
                    port=port,
                    endpoints=("/api/v1/overview", "/api/v1/candidate-notifications"),
                    deadline_seconds=self.readiness_deadline,
                    interval_seconds=self.readiness_interval,
                )
                if not health_ok:
                    self._recover_managed_current(
                        current_sha=current_state.current,
                        unit_file=target_unit,
                        previous_unit_text=previous_unit_text,
                        port=port,
                        health_endpoints=(
                            "/api/v1/overview",
                            "/api/v1/candidate-notifications",
                        ),
                        failure_message=(
                            f"Health check failed during rollback to {prev_sha} with "
                            f"status {status_code}"
                        ),
                        failure=ReleaseVerificationError(
                            f"Health check failed during rollback to {prev_sha}"
                        ),
                    )

        new_state = DeploymentState(
            schema_version="local-deployment-state-v1",
            current=prev_sha,
            previous=current_state.current,
            updated_at=datetime.now(UTC),
            current_release_path=str(prev_release_dir),
            previous_release_path=current_state.current_release_path,
        )
        self._save_deployment_state(new_state)
        return new_state

    def status(
        self,
        unit_file: Path | None = None,
        port: int = 8765,
        check_systemd: bool = True,
        check_http: bool = True,
    ) -> ReleaseStatusReport:
        """Inspect and report the status of the local release runtime and unit from systemd."""
        state = self.load_deployment_state()
        target_unit = (
            unit_file.expanduser().resolve(strict=False)
            if unit_file is not None
            else self.systemd_unit_path
        )

        working_dir: str | None = None
        exec_start: str | None = None
        env_file: str | None = None
        service_active: bool | None = None
        service_enabled: bool | None = None
        unit_matches_current = False

        if check_systemd:
            # Strictly fail-closed: query live loaded properties directly from systemd
            try:
                props = self.systemctl.show_properties(
                    LOCAL_SERVICE_UNIT_NAME,
                    (
                        "WorkingDirectory",
                        "ExecStart",
                        "EnvironmentFile",
                        "ActiveState",
                        "UnitFileState",
                    ),
                )
                loaded_wd = props.get("WorkingDirectory") or None
                loaded_exec = props.get("ExecStart") or None
                loaded_env = props.get("EnvironmentFile") or None
                active_state = props.get("ActiveState") or None
                unit_file_state = props.get("UnitFileState") or None

                working_dir = loaded_wd
                exec_start = loaded_exec
                env_file = loaded_env

                if active_state:
                    service_active = active_state == "active"
                else:
                    service_active = self.systemctl.is_active(LOCAL_SERVICE_UNIT_NAME)

                if unit_file_state:
                    service_enabled = unit_file_state == "enabled"
                else:
                    service_enabled = self.systemctl.is_enabled(LOCAL_SERVICE_UNIT_NAME)

                # unit_matches_current MUST be demonstrated by loaded systemd properties
                if (
                    state.current is not None
                    and loaded_wd is not None
                    and loaded_exec is not None
                    and f"releases/{state.current}" in loaded_wd
                    and f"releases/{state.current}" in loaded_exec
                ):
                    unit_matches_current = True
                else:
                    unit_matches_current = False
            except Exception:
                # Systemd query failed -> fail closed
                service_active = False
                service_enabled = False
                unit_matches_current = False
        else:
            # Static check from unit file on disk
            if target_unit.is_file():
                with contextlib.suppress(OSError):
                    for line in target_unit.read_text(encoding="utf-8").splitlines():
                        stripped = line.strip()
                        if stripped.startswith("WorkingDirectory="):
                            working_dir = stripped.split("=", 1)[1].strip()
                        elif stripped.startswith("ExecStart="):
                            exec_start = stripped.split("=", 1)[1].strip()
                        elif stripped.startswith("EnvironmentFile="):
                            env_file = stripped.split("=", 1)[1].strip()

                if (
                    state.current is not None
                    and working_dir is not None
                    and f"releases/{state.current}" in working_dir
                ):
                    unit_matches_current = True

        manifest: ReleaseManifest | None = None
        if state.current is not None:
            manifest_path = self.paths.releases / state.current / "manifest.json"
            if manifest_path.is_file():
                with contextlib.suppress(Exception):
                    manifest = ReleaseManifest.model_validate_json(
                        manifest_path.read_text(encoding="utf-8")
                    )

        overview_status: int | None = None
        if check_http:
            _, overview_status = self.health_checker.check_health(
                port=port, endpoints=("/api/v1/overview",)
            )

        return ReleaseStatusReport(
            schema_version="local-release-status-report-v1",
            current_commit=state.current,
            previous_commit=state.previous,
            current_release_dir=state.current_release_path,
            current_manifest=manifest,
            unit_path=str(target_unit),
            unit_working_directory=working_dir,
            unit_exec_start=exec_start,
            unit_environment_file=env_file,
            unit_matches_current=unit_matches_current,
            service_active=service_active,
            service_enabled=service_enabled,
            overview_status=overview_status,
        )
