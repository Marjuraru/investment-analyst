"""Integration coverage for workspace and legacy CLI composition without network access."""

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_PROJECT_ROOT = Path(__file__).parents[3]
_QUERY_SCRIPT = _PROJECT_ROOT / "scripts" / "query_aapl_diagnostics.py"
_SEC_SCRIPT = _PROJECT_ROOT / "scripts" / "fetch_sec_aapl_fundamentals.py"
_SUBMISSIONS = _PROJECT_ROOT / "tests" / "fixtures" / "sec" / "aapl_submissions.json"
_COMPANY_FACTS = _PROJECT_ROOT / "tests" / "fixtures" / "sec" / "aapl_companyfacts.json"


def _environment(workspace: Path | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(_PROJECT_ROOT / "src"), environment.get("PYTHONPATH", "")]
    )
    if workspace is None:
        environment.pop("INVESTMENT_ANALYST_WORKSPACE", None)
    else:
        environment["INVESTMENT_ANALYST_WORKSPACE"] = str(workspace)
    return environment


def _query(*location: str, workspace: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_QUERY_SCRIPT),
            *location,
            "--known-at",
            "2026-07-14T06:46:00Z",
            "--fundamental-frequency",
            "quarterly",
        ],
        cwd=Path.home(),
        env=_environment(workspace),
        check=False,
        capture_output=True,
        text=True,
    )


def _fetch_sec(*location: str, workspace: Path | None = None) -> subprocess.CompletedProcess[str]:
    launcher = "\n".join(
        (
            "import runpy, sys",
            "from pathlib import Path",
            "from investment_analyst.providers.http import HttpResponse",
            "import investment_analyst.providers.http as http_module",
            "class FakeTransport:",
            "    def __init__(self, *args, **kwargs): pass",
            "    def get(self, url, *, headers, timeout_seconds):",
            f"        submissions = Path({str(_SUBMISSIONS)!r}).read_bytes()",
            f"        company = Path({str(_COMPANY_FACTS)!r}).read_bytes()",
            "        body = submissions if '/submissions/' in url else company",
            "        return HttpResponse(status_code=200, body=body, headers={}, url=url)",
            "http_module.UrlLibHttpTransport = FakeTransport",
            "sys.argv = ['fetch_sec_aapl_fundamentals.py', *sys.argv[1:]]",
            f"runpy.run_path({str(_SEC_SCRIPT)!r}, run_name='__main__')",
        )
    )
    environment = _environment(workspace)
    environment["SEC_USER_AGENT"] = "Runtime integration test@example.com"
    return subprocess.run(
        [sys.executable, "-c", launcher, *location],
        cwd=Path.home(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_read_only_query_supports_legacy_workspace_and_default(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    with LocalStorage(StoragePaths.from_root(legacy)):
        pass
    workspace = tmp_path / "workspace"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)

    legacy_database = StoragePaths.from_root(legacy).database_path
    workspace_database = StoragePaths.from_root(workspace / "storage").database_path
    legacy_before = _digest(legacy_database)
    workspace_before = _digest(workspace_database)

    legacy_result = _query("--root", str(legacy))
    workspace_result = _query("--workspace", str(workspace))
    default_result = _query(workspace=workspace)

    assert legacy_result.returncode == 0, legacy_result.stderr
    assert workspace_result.returncode == 0, workspace_result.stderr
    assert default_result.returncode == 0, default_result.stderr
    legacy_payload = json.loads(legacy_result.stdout)
    workspace_payload = json.loads(workspace_result.stdout)
    default_payload = json.loads(default_result.stdout)
    assert legacy_payload == workspace_payload == default_payload
    assert legacy_payload["status"] == "unavailable"
    assert _digest(legacy_database) == legacy_before
    assert _digest(workspace_database) == workspace_before


def test_read_write_sec_import_is_equivalent_and_persists(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    workspace = tmp_path / "workspace"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)

    legacy_result = _fetch_sec("--root", str(legacy))
    workspace_result = _fetch_sec("--workspace", str(workspace))

    assert legacy_result.returncode == 0, legacy_result.stderr
    assert workspace_result.returncode == 0, workspace_result.stderr
    legacy_summary = json.loads(legacy_result.stdout)["summary"]
    workspace_summary = json.loads(workspace_result.stdout)["summary"]
    comparable_keys = (
        "asset_id",
        "cik",
        "documents_received",
        "submissions_record_id",
        "companyfacts_record_id",
        "raw_records_created",
        "traceability_verified",
    )
    assert {key: legacy_summary[key] for key in comparable_keys} == {
        key: workspace_summary[key] for key in comparable_keys
    }
    with LocalStorage(StoragePaths.from_root(legacy), read_only=True) as storage:
        assert len(storage.raw_records.list()) == 2
    with LocalStorage(StoragePaths.from_root(workspace / "storage"), read_only=True) as storage:
        assert len(storage.raw_records.list()) == 2


def test_conflicting_locations_fail_in_argparse_without_traceback(tmp_path: Path) -> None:
    result = _query(
        "--workspace",
        str(tmp_path / "workspace"),
        "--root",
        str(tmp_path / "legacy"),
    )

    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_read_only_legacy_root_fails_without_creating_files(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    result = _query("--root", str(root))

    assert result.returncode != 0
    assert "not initialized" in result.stderr
    assert "Traceback" not in result.stderr
    assert not root.exists()


def _run_command(
    script_name: str,
    arguments: tuple[str, ...],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PROJECT_ROOT / "scripts" / script_name), *arguments],
        cwd=Path.home(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_provider_writers_fail_compactly_for_missing_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "missing-workspace"
    environment = _environment()
    environment.update(
        {
            "ALPACA_API_KEY": "runtime-key",
            "ALPACA_API_SECRET": "runtime-secret",
            "SEC_USER_AGENT": "Runtime boundary test@example.com",
        }
    )
    commands = (
        (
            "fetch_alpaca_history.py",
            (
                "--workspace",
                str(missing),
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-02",
            ),
        ),
        (
            "fetch_coinbase_history.py",
            (
                "--workspace",
                str(missing),
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-02",
            ),
        ),
        ("fetch_sec_aapl_fundamentals.py", ("--workspace", str(missing))),
        (
            "run_aapl_complete_snapshot.py",
            (
                "--workspace",
                str(missing),
                "--known-at",
                "2026-07-14T06:46:00Z",
                "--market-start",
                "2026-07-01",
                "--market-end",
                "2026-07-02",
                "--fundamental-frequency",
                "quarterly",
            ),
        ),
    )

    for script_name, arguments in commands:
        result = _run_command(script_name, arguments, environment=environment)
        combined = result.stdout + result.stderr
        assert result.returncode != 0
        assert "manifest was not found" in combined
        assert "Traceback" not in combined
        assert "runtime-key" not in combined
        assert "runtime-secret" not in combined
        assert not missing.exists()


def test_snapshot_rejects_incompatible_workspace_without_traceback(tmp_path: Path) -> None:
    workspace = tmp_path / "incompatible"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)
    manifest = workspace / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["application"] = "another-application"
    manifest.write_text(f"{json.dumps(document, sort_keys=True)}\n", encoding="utf-8")
    environment = _environment()
    environment.update(
        {
            "ALPACA_API_KEY": "runtime-key",
            "ALPACA_API_SECRET": "runtime-secret",
        }
    )

    result = _run_command(
        "run_aapl_complete_snapshot.py",
        (
            "--workspace",
            str(workspace),
            "--known-at",
            "2026-07-14T06:46:00Z",
            "--market-start",
            "2026-07-01",
            "--market-end",
            "2026-07-02",
            "--fundamental-frequency",
            "quarterly",
        ),
        environment=environment,
    )

    assert result.returncode != 0
    assert "another application" in result.stderr
    assert "Traceback" not in result.stderr
    assert "runtime-key" not in result.stderr
    assert "runtime-secret" not in result.stderr


def test_sec_writer_maps_workspace_lock_without_network_or_traceback(tmp_path: Path) -> None:
    workspace = tmp_path / "locked"
    WorkspaceService(environ={}, home=tmp_path).initialize(workspace)
    launcher = "\n".join(
        (
            "import runpy, sys",
            (
                "from investment_analyst.workspace.service import "
                "WorkspaceLockedError, WorkspaceService"
            ),
            "def locked(self, paths, mode):",
            "    raise WorkspaceLockedError('workspace storage is locked by another process')",
            "WorkspaceService.open_storage = locked",
            "sys.argv = ['fetch_sec_aapl_fundamentals.py', *sys.argv[1:]]",
            f"runpy.run_path({str(_SEC_SCRIPT)!r}, run_name='__main__')",
        )
    )
    environment = _environment()
    environment["SEC_USER_AGENT"] = "Runtime lock test@example.com"

    result = subprocess.run(
        [sys.executable, "-c", launcher, "--workspace", str(workspace)],
        cwd=Path.home(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "locked by another process" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Runtime lock test@example.com" not in result.stdout + result.stderr
