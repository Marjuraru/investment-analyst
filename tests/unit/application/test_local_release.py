"""Unit tests for independent local release runtime, acquisition, and management."""

import hashlib
import io
import stat
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.aapl_scheduler import AaplDailyScheduleConfig
from investment_analyst.application.local_release import (
    DeploymentState,
    LocalReleaseService,
    RealHealthChecker,
    ReleaseAcquisitionError,
    ReleaseConfigurationError,
    ReleaseEnvironmentError,
    ReleaseManifest,
    ReleaseRollbackError,
    ReleaseUnitError,
    ReleaseVerificationError,
    SimulatedHealthChecker,
    SimulatedSystemctlRunner,
    _safe_tar_extract,
)
from investment_analyst.application.local_service_unit import (
    AaplLocalServiceUnitConfig,
    render_local_service_unit,
    write_local_service_unit,
)
from investment_analyst.application.operational_state import AaplDailyRunLock
from investment_analyst.core.models import DataFrequency


def test_full_sha_validation_and_rejection() -> None:
    """Validate 40-character lowercase hex SHAs and reject invalid variants."""
    valid_commit = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    valid_tree = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"
    valid_lock = hashlib.sha256(b"dummy").hexdigest()

    manifest = ReleaseManifest(
        schema_version="local-release-manifest-v1",
        commit_sha=valid_commit,
        tree_sha=valid_tree,
        uv_lock_sha256=valid_lock,
        python_version="Python 3.12.3",
        staged_at=datetime.now(UTC),
        release_path="/tmp/releases/1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb",
    )
    assert manifest.commit_sha == valid_commit
    assert manifest.tree_sha == valid_tree

    for bad_sha in (
        "1b0a1ab",
        "not-a-sha-at-all",
        "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbbg",
        "",
    ):
        with pytest.raises(ValueError):
            ReleaseManifest(
                schema_version="local-release-manifest-v1",
                commit_sha=bad_sha,
                tree_sha=valid_tree,
                uv_lock_sha256=valid_lock,
                python_version="Python 3.12.3",
                staged_at=datetime.now(UTC),
                release_path="/tmp/release",
            )


def test_environment_file_validation_and_adoption(tmp_path: Path) -> None:
    """Validate private environment adoption, permissions, and byte-exact copies."""
    service_dir = tmp_path / "runtime"
    service_env = tmp_path / "config" / "service.env"
    service = LocalReleaseService(paths=service_dir, service_env_path=service_env)

    # 1. Reject non-existent source
    with pytest.raises(ReleaseConfigurationError, match="does not exist"):
        service.adopt_env(tmp_path / "missing.env")

    # 2. Reject loose permissions (e.g. 0644)
    env_file = tmp_path / "source.env"
    env_file.write_text(
        "ALPACA_API_KEY=key123\nALPACA_API_SECRET=sec456\nSEC_USER_AGENT=Agent user@mail.com\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)
    with pytest.raises(ReleaseConfigurationError, match="0600 or more restrictive"):
        service.adopt_env(env_file)

    # 3. Set strict permissions (0600)
    env_file.chmod(0o600)

    # 4. Reject missing required variables
    bad_env = tmp_path / "bad.env"
    bad_env.write_text("ALPACA_API_KEY=key123\n", encoding="utf-8")
    bad_env.chmod(0o600)
    with pytest.raises(ReleaseConfigurationError, match="missing required non-empty variables"):
        service.adopt_env(bad_env)

    # 5. Reject duplicate variables
    dup_env = tmp_path / "dup.env"
    dup_env.write_text(
        "ALPACA_API_KEY=key1\nALPACA_API_KEY=key2\nALPACA_API_SECRET=sec\nSEC_USER_AGENT=agent\n",
        encoding="utf-8",
    )
    dup_env.chmod(0o600)
    with pytest.raises(ReleaseConfigurationError, match="duplicate variable"):
        service.adopt_env(dup_env)

    # 6. Reject invalid syntax
    syntax_env = tmp_path / "syntax.env"
    syntax_env.write_text("export ALPACA_API_KEY=key\n", encoding="utf-8")
    syntax_env.chmod(0o600)
    with pytest.raises(ReleaseConfigurationError, match="only NAME=value entries"):
        service.adopt_env(syntax_env)

    # 7. Successful adoption
    adopted = service.adopt_env(env_file)
    assert adopted == service_env
    assert adopted.is_file()
    assert stat.S_IMODE(adopted.stat().st_mode) == 0o600
    assert adopted.read_bytes() == env_file.read_bytes()

    # 8. Idempotent repeat adoption with identical content
    repeated = service.adopt_env(env_file)
    assert repeated == service_env

    # 9. Rejection if destination exists with different content
    different_source = tmp_path / "diff.env"
    different_source.write_text(
        "ALPACA_API_KEY=different_key\nALPACA_API_SECRET=different_sec\nSEC_USER_AGENT=Different\n",
        encoding="utf-8",
    )
    different_source.chmod(0o600)
    with pytest.raises(ReleaseConfigurationError, match="already exists with different content"):
        service.adopt_env(different_source)


def test_unit_retargeting_exact_and_fail_closed(tmp_path: Path) -> None:
    """Test exact retargeting of systemd unit preserving all operational arguments."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    runtime_root = tmp_path / "runtime"
    release_dir = runtime_root / "releases" / sha
    (release_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (release_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (release_dir / ".venv" / "bin" / "python").touch()
    (release_dir / "scripts" / "serve_investment_analyst.py").touch()

    env_file = tmp_path / "config" / "service.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "ALPACA_API_KEY=key\nALPACA_API_SECRET=sec\nSEC_USER_AGENT=agent\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    unit_config = AaplLocalServiceUnitConfig(
        repository_root=tmp_path / "old_repo",
        environment_file=tmp_path / "old_repo" / ".env",
        workspace_root=tmp_path / "workspace",
        port=8765,
        schedule=AaplDailyScheduleConfig(
            timezone="America/Lima",
            run_at="07:30",
            market_start="2025-01-01",
            market_end_lag_days=1,
            fundamental_frequency=DataFrequency.QUARTERLY,
            refresh_mode=AaplRefreshMode.AUTO,
            require_complete=True,
        ),
        scheduled_asset_ids=("crypto:btc-usd", "equity:us:aapl"),
        schedule_intraday=False,
        schedule_smv_registry=True,
        schedule_macro=False,
    )
    rendered = render_local_service_unit(unit_config)
    unit_file = tmp_path / "systemd" / "investment-analyst.service"
    write_local_service_unit(unit_file, rendered)

    service = LocalReleaseService(
        paths=runtime_root,
        systemd_unit_path=unit_file,
        service_env_path=env_file,
    )

    updated = service.retarget_unit(sha)
    assert f"WorkingDirectory={str(release_dir)}" in updated
    assert f"EnvironmentFile={str(env_file)}" in updated
    expected_py = str(release_dir / ".venv" / "bin" / "python")
    expected_script = str(release_dir / "scripts" / "serve_investment_analyst.py")
    assert f'ExecStart="{expected_py}" "{expected_script}"' in updated
    assert '"--workspace"' in updated
    assert f'"{str(tmp_path / "workspace")}"' in updated
    assert '"--port" "8765"' in updated
    assert '"--schedule-at" "07:30"' in updated
    assert '"--timezone" "America/Lima"' in updated
    assert '"--schedule-asset" "crypto:btc-usd"' in updated
    assert '"--schedule-asset" "equity:us:aapl"' in updated
    assert '"--no-schedule-intraday"' in updated
    assert '"--no-schedule-macro"' in updated

    bad_unit = tmp_path / "bad.service"
    bad_unit.write_text("[Unit]\nDescription=Bad\n", encoding="utf-8")
    with pytest.raises(ReleaseUnitError, match="missing"):
        service.retarget_unit(sha, unit_file=bad_unit)


def test_safe_tar_extract_prevents_path_traversal(tmp_path: Path) -> None:
    """Verify archive extraction refuses directory traversal and unsafe members."""
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"malicious content"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    buf.seek(0)
    with pytest.raises(ReleaseAcquisitionError, match="Unsafe path traversal"):
        _safe_tar_extract(buf.getvalue(), dest_dir)


def test_stage_release_and_manifest_determinism(tmp_path: Path) -> None:
    """Test staging a release, manifest generation, and idempotence."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    tree_sha = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"
    runtime_root = tmp_path / "runtime"

    service = LocalReleaseService(paths=runtime_root)

    with (
        patch.object(service, "fetch_origin_main", return_value=(sha, tree_sha)),
        patch.object(service, "ensure_uv", return_value=Path("/bin/uv")),
        patch("subprocess.run") as mock_run,
    ):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, content in (
                ("pyproject.toml", b'[project]\nname="investment-analyst"\n'),
                ("uv.lock", b"version = 1\n"),
                ("scripts/serve_investment_analyst.py", b"# server script\n"),
                ("src/investment_analyst/__init__.py", b"# init\n"),
            ):
                data = content
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        def side_effect(cmd, *args, **kwargs):
            if "archive" in cmd:
                return MagicMock(returncode=0, stdout=buf.getvalue())
            if "sync" in cmd or "venv" in cmd:
                staging_dirs = list((runtime_root / "releases").glob(".staging-*"))
                if staging_dirs:
                    py_bin = staging_dirs[0] / ".venv" / "bin" / "python"
                    py_bin.parent.mkdir(parents=True, exist_ok=True)
                    py_bin.touch()
                return MagicMock(returncode=0, stdout="")
            if "--version" in cmd:
                return MagicMock(returncode=0, stdout="Python 3.12.3\n")
            if "import investment_analyst" in cmd:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = side_effect

        manifest = service.stage(sha)
        assert manifest.commit_sha == sha
        assert manifest.tree_sha == tree_sha
        assert (runtime_root / "releases" / sha / "manifest.json").is_file()

        repeat_manifest = service.stage(sha)
        assert repeat_manifest.commit_sha == sha


def test_stage_rejects_non_python_312(tmp_path: Path) -> None:
    """Verify that stage refuses any virtualenv built with non-3.12 Python."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    tree_sha = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"
    runtime_root = tmp_path / "runtime"
    service = LocalReleaseService(paths=runtime_root)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in (
            ("pyproject.toml", b'[project]\nname="investment-analyst"\n'),
            ("uv.lock", b"version = 1\n"),
            ("scripts/serve_investment_analyst.py", b"# server\n"),
        ):
            data = content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    def mock_run(cmd, *args, **kwargs):
        if "archive" in cmd:
            return MagicMock(returncode=0, stdout=buf.getvalue())
        if "sync" in cmd or "venv" in cmd:
            staging_dirs = list((runtime_root / "releases").glob(".staging-*"))
            if staging_dirs:
                py_bin = staging_dirs[0] / ".venv" / "bin" / "python"
                py_bin.parent.mkdir(parents=True, exist_ok=True)
                py_bin.touch()
            return MagicMock(returncode=0, stdout="")
        if "--version" in cmd:
            return MagicMock(returncode=0, stdout="Python 3.11.8\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch.object(service, "fetch_origin_main", return_value=(sha, tree_sha)),
        patch.object(service, "ensure_uv", return_value=Path("/bin/uv")),
        patch("subprocess.run", side_effect=mock_run),
        pytest.raises(ReleaseEnvironmentError, match="must use Python 3.12"),
    ):
        service.stage(sha)


def test_fetch_origin_main_rejects_moving_remote_ref(tmp_path: Path) -> None:
    """Verify that fetch_origin_main rejects remote origin/main moving during acquisition."""
    sha1 = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    sha2 = "2222222222222222222222222222222222222222"
    runtime_root = tmp_path / "runtime"
    service = LocalReleaseService(paths=runtime_root)

    query_calls = [sha1, sha2]

    def mock_query():
        return query_calls.pop(0)

    with (
        patch.object(service, "_query_remote_main", side_effect=mock_query),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        pytest.raises(ReleaseAcquisitionError, match="Remote origin/main moved during acquisition"),
    ):
        service.fetch_origin_main(sha1)


def test_preexisting_release_non_equivalent_tampered_lock_or_corrupt_rejection(
    tmp_path: Path,
) -> None:
    """Verify preexisting release equivalence detects tampered lock, bad python or corruption."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    tree_sha = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"
    runtime_root = tmp_path / "runtime"
    r_dir = runtime_root / "releases" / sha
    r_dir.mkdir(parents=True, exist_ok=True)
    (r_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (r_dir / "scripts").mkdir(parents=True, exist_ok=True)

    py_bin = r_dir / ".venv" / "bin" / "python"
    py_bin.touch()
    pyproject = r_dir / "pyproject.toml"
    pyproject.write_text("[project]\nname='test'\n", encoding="utf-8")
    uv_lock = r_dir / "uv.lock"
    uv_lock.write_text("lock-content-original\n", encoding="utf-8")
    orig_lock_hash = hashlib.sha256(b"lock-content-original\n").hexdigest()
    server_script = r_dir / "scripts" / "serve_investment_analyst.py"
    server_script.touch()

    manifest = ReleaseManifest(
        schema_version="local-release-manifest-v1",
        commit_sha=sha,
        tree_sha=tree_sha,
        uv_lock_sha256=orig_lock_hash,
        python_version="Python 3.12.3",
        staged_at=datetime.now(UTC),
        release_path=str(r_dir),
    )
    (r_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    service = LocalReleaseService(paths=runtime_root)

    # 1. Tamper uv.lock on disk
    uv_lock.write_text("lock-content-tampered\n", encoding="utf-8")
    with (
        patch.object(service, "fetch_origin_main", return_value=(sha, tree_sha)),
        pytest.raises(ReleaseAcquisitionError, match="not equivalent"),
    ):
        service.stage(sha)

    # Restore uv.lock and tamper python version check
    uv_lock.write_text("lock-content-original\n", encoding="utf-8")

    def mock_run_bad_py(cmd, *args, **kwargs):
        if "--version" in cmd:
            return MagicMock(returncode=0, stdout="Python 3.11.5\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch.object(service, "fetch_origin_main", return_value=(sha, tree_sha)),
        patch("subprocess.run", side_effect=mock_run_bad_py),
        pytest.raises(ReleaseAcquisitionError, match="not Python 3.12"),
    ):
        service.stage(sha)

    release_snapshot = {
        path: path.read_bytes()
        for path in (pyproject, uv_lock, server_script, r_dir / "manifest.json")
    }

    def mock_run_good_py(cmd, *args, **kwargs):
        if "--version" in cmd:
            return MagicMock(returncode=0, stdout="Python 3.12.3\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch.object(service, "fetch_origin_main", return_value=(sha, tree_sha)),
        patch.object(
            service,
            "_validate_installed_runtime",
            side_effect=ReleaseAcquisitionError("installed probe failed"),
        ),
        patch("subprocess.run", side_effect=mock_run_good_py),
        pytest.raises(ReleaseAcquisitionError, match="installed probe failed"),
    ):
        service.stage(sha)

    assert {path: path.read_bytes() for path in release_snapshot} == release_snapshot


def test_pre_restart_verification_and_writer_lock_rejection(tmp_path: Path) -> None:
    """Verify pre-restart checks reject active writer locks, invalid ports, and unstaged SHAs."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    runtime_root = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = workspace / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    r_dir = runtime_root / "releases" / sha
    (r_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (r_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (r_dir / ".venv" / "bin" / "python").touch()
    (r_dir / "scripts" / "serve_investment_analyst.py").touch()
    manifest = ReleaseManifest(
        schema_version="local-release-manifest-v1",
        commit_sha=sha,
        tree_sha="b6f8115ffdddb0915cae50736dbc821c5355d3ac",
        uv_lock_sha256=hashlib.sha256(b"dummy").hexdigest(),
        python_version="Python 3.12.3",
        staged_at=datetime.now(UTC),
        release_path=str(r_dir),
    )
    (r_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    unit_file = tmp_path / "systemd" / "investment-analyst.service"
    unit_content = (
        "[Service]\n"
        f"WorkingDirectory={r_dir}\n"
        f'ExecStart="/bin/py" "/bin/scr" --workspace "{workspace}" --port 8765\n'
        f"EnvironmentFile=/etc/env\n"
    )
    write_local_service_unit(unit_file, unit_content)

    systemctl = SimulatedSystemctlRunner()
    service = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl,
        systemd_unit_path=unit_file,
    )

    # 1. Clean workspace with no lock file passes
    service.verify_pre_restart(sha=sha, unit_file=unit_file, workspace_root=workspace)

    # 2. Acquire AaplDailyRunLock: active writer is detected and rejected
    daily_lock_path = state_dir / "aapl_daily_run.lock"
    with (
        AaplDailyRunLock(daily_lock_path, run_id=uuid4(), started_at="2026-08-16T00:00:00Z"),
        pytest.raises(ReleaseVerificationError, match="Active writer operation detected"),
    ):
        service.verify_pre_restart(sha=sha, unit_file=unit_file, workspace_root=workspace)

    # 3. Lock released: stable file still persists on disk, but flock is free -> PASSES!
    assert daily_lock_path.is_file()
    service.verify_pre_restart(sha=sha, unit_file=unit_file, workspace_root=workspace)

    # 4. Rejection on invalid port
    with pytest.raises(ReleaseVerificationError, match="Port must be an integer"):
        service.verify_pre_restart(sha=sha, unit_file=unit_file, port=999999)

    # 5. Rejection on unstaged SHA
    with pytest.raises(ReleaseVerificationError, match="not fully staged"):
        service.verify_pre_restart(
            sha="2222222222222222222222222222222222222222", unit_file=unit_file
        )


def test_status_unit_matches_current_from_loaded_systemd_properties(tmp_path: Path) -> None:
    """Verify status and unit_matches_current fail closed on systemd property anomalies."""
    runtime_root = tmp_path / "runtime"
    sha1 = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    sha2 = "2222222222222222222222222222222222222222"

    for s in (sha1, sha2):
        r_dir = runtime_root / "releases" / s
        r_dir.mkdir(parents=True, exist_ok=True)
        manifest = ReleaseManifest(
            schema_version="local-release-manifest-v1",
            commit_sha=s,
            tree_sha="b6f8115ffdddb0915cae50736dbc821c5355d3ac",
            uv_lock_sha256=hashlib.sha256(b"dummy").hexdigest(),
            python_version="Python 3.12.3",
            staged_at=datetime.now(UTC),
            release_path=str(r_dir),
        )
        (r_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    state = DeploymentState(
        schema_version="local-deployment-state-v1",
        current=sha1,
        previous=None,
        updated_at=datetime.now(UTC),
        current_release_path=str(runtime_root / "releases" / sha1),
        previous_release_path=None,
    )
    (runtime_root / "deployment_state.json").write_text(state.model_dump_json(), encoding="utf-8")

    unit_file = tmp_path / "unit.service"
    unit_file.write_text(
        f"[Service]\nWorkingDirectory={runtime_root / 'releases' / sha1}\nExecStart=/py /scr\n",
        encoding="utf-8",
    )

    # 1. Systemd loaded properties match current release sha1 -> PASS
    systemctl_matching = SimulatedSystemctlRunner(
        is_active_result=True,
        is_enabled_result=True,
        properties={
            "WorkingDirectory": str(runtime_root / "releases" / sha1),
            "ExecStart": f'"{runtime_root / "releases" / sha1 / ".venv/bin/python"}" "/scr"',
            "ActiveState": "active",
            "UnitFileState": "enabled",
        },
    )
    service_matching = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl_matching,
        systemd_unit_path=unit_file,
    )
    report_matching = service_matching.status(check_systemd=True, check_http=False)
    assert report_matching.unit_matches_current is True
    assert report_matching.service_active is True
    assert report_matching.service_enabled is True

    # 2. Systemd loaded properties point to stale sha2 -> unit_matches_current is False
    systemctl_stale = SimulatedSystemctlRunner(
        is_active_result=True,
        is_enabled_result=True,
        properties={
            "WorkingDirectory": str(runtime_root / "releases" / sha2),
            "ExecStart": f'"{runtime_root / "releases" / sha2 / ".venv/bin/python"}" "/scr"',
            "ActiveState": "active",
            "UnitFileState": "enabled",
        },
    )
    service_stale = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl_stale,
        systemd_unit_path=unit_file,
    )
    report_stale = service_stale.status(check_systemd=True, check_http=False)
    assert report_stale.unit_matches_current is False

    # 3. Systemctl show fails completely (fail-closed: does not fall back to unit file)
    systemctl_failed = SimulatedSystemctlRunner(
        is_active_result=True,
        is_enabled_result=True,
        fail_show=True,
    )
    service_failed = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl_failed,
        systemd_unit_path=unit_file,
    )
    report_failed = service_failed.status(check_systemd=True, check_http=False)
    assert report_failed.unit_matches_current is False
    assert report_failed.service_active is False

    # 4. Systemctl show returns incomplete properties (e.g. missing ExecStart) -> fail closed
    systemctl_incomplete = SimulatedSystemctlRunner(
        is_active_result=True,
        is_enabled_result=True,
        properties={
            "WorkingDirectory": str(runtime_root / "releases" / sha1),
            "ActiveState": "active",
        },
    )
    service_incomplete = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl_incomplete,
        systemd_unit_path=unit_file,
    )
    report_incomplete = service_incomplete.status(check_systemd=True, check_http=False)
    assert report_incomplete.unit_matches_current is False


def test_activation_automatic_rollback_verifies_recovery_health_and_fail_closed(
    tmp_path: Path,
) -> None:
    """Verify that rollback during failed activation validates recovery health and fails closed."""
    sha1 = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    sha2 = "2222222222222222222222222222222222222222"
    runtime_root = tmp_path / "runtime"

    for s in (sha1, sha2):
        r_dir = runtime_root / "releases" / s
        (r_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (r_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (r_dir / ".venv" / "bin" / "python").touch()
        (r_dir / "scripts" / "serve_investment_analyst.py").touch()
        manifest = ReleaseManifest(
            schema_version="local-release-manifest-v1",
            commit_sha=s,
            tree_sha="b6f8115ffdddb0915cae50736dbc821c5355d3ac",
            uv_lock_sha256=hashlib.sha256(b"dummy").hexdigest(),
            python_version="Python 3.12.3",
            staged_at=datetime.now(UTC),
            release_path=str(r_dir),
        )
        (r_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    env_file = tmp_path / "config" / "service.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "ALPACA_API_KEY=k\nALPACA_API_SECRET=s\nSEC_USER_AGENT=u\n", encoding="utf-8"
    )
    env_file.chmod(0o600)

    unit_file = tmp_path / "systemd" / "investment-analyst.service"
    sha1_py = runtime_root / "releases" / sha1 / ".venv" / "bin" / "python"
    sha1_scr = runtime_root / "releases" / sha1 / "scripts" / "serve_investment_analyst.py"
    unit_content = (
        "[Unit]\nDescription=Test\n\n"
        "[Service]\n"
        f"WorkingDirectory={runtime_root / 'releases' / sha1}\n"
        f"EnvironmentFile={env_file}\n"
        f'ExecStart="{sha1_py}" "{sha1_scr}" --port 8765\n\n'
        "[Install]\nWantedBy=default.target\n"
    )
    write_local_service_unit(unit_file, unit_content)

    systemctl = SimulatedSystemctlRunner()
    health_checker = SimulatedHealthChecker(succeed=True)

    service = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl,
        health_checker=health_checker,
        systemd_unit_path=unit_file,
        service_env_path=env_file,
    )

    # Initial activation of sha1 succeeds
    service.activate(sha1)

    # Case A: Activation of sha2 fails health, recovery health check on sha1 succeeds
    health_checker.succeed = False

    class RecoveryHealthChecker:
        def __init__(self):
            self.calls = 0

        def check_health(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return False, 500  # sha2 activation fails
            return True, 200  # sha1 recovery succeeds

        def check_health_readiness(self, *args, **kwargs):
            return self.check_health(*args, **kwargs)

    service.health_checker = RecoveryHealthChecker()
    with pytest.raises(
        ReleaseRollbackError, match="successfully rolled back and verified"
    ) as error:
        service.activate(sha2)
    assert sha1 in str(error.value)
    assert "None" not in str(error.value)
    assert service.load_deployment_state().current == sha1
    assert sha1 in unit_file.read_text(encoding="utf-8")

    # Case B: Activation of sha2 fails health, AND recovery on sha1 ALSO fails
    class TotalFailureHealthChecker:
        def check_health(self, *args, **kwargs):
            return False, 500

        def check_health_readiness(self, *args, **kwargs):
            return self.check_health(*args, **kwargs)

    service.health_checker = TotalFailureHealthChecker()
    with pytest.raises(ReleaseRollbackError, match="CRITICAL: Health check failed.*ALSO FAILED"):
        service.activate(sha2)


def test_activation_restart_failure_recovers_managed_current_exact_sha(tmp_path: Path) -> None:
    """A restart failure restores and verifies the managed current, not previous/null."""
    sha_current = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    sha_target = "2222222222222222222222222222222222222222"
    service, unit_file, _env = _make_readiness_test_service(
        tmp_path,
        sha_target,
        sha_current,
        SimulatedHealthChecker(succeed=True),
        readiness_deadline=1.0,
    )

    assert isinstance(service.systemctl, SimulatedSystemctlRunner)
    original_restart = service.systemctl.restart
    restart_attempts = 0

    def fail_candidate_restart(service_name: str) -> None:
        nonlocal restart_attempts
        restart_attempts += 1
        if restart_attempts == 1:
            raise ReleaseUnitError("candidate restart failed")
        original_restart(service_name)

    service.systemctl.restart = fail_candidate_restart

    with pytest.raises(ReleaseRollbackError, match="managed current") as error:
        service.activate(sha_target)

    assert sha_current in str(error.value)
    assert "previous release" not in str(error.value)
    assert service.load_deployment_state().current == sha_current
    assert sha_current in unit_file.read_text(encoding="utf-8")
    assert restart_attempts == 2


class DeterministicClock:
    """Deterministic simulated clock and sleeper for readiness polling tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.current_time = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.current_time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current_time += seconds


def test_readiness_allows_cold_start_before_default_deadline() -> None:
    """The 120-second default leaves room for the observed ~68-second cold start."""
    clock = DeterministicClock()
    health = SimulatedHealthChecker(
        responses=[(False, None)] * 272 + [(True, 200)],
        clock=clock.clock,
        sleeper=clock.sleep,
    )

    ok, status = health.check_health_readiness(port=8765)

    assert ok is True
    assert status == 200
    assert health.call_count == 273
    assert clock.current_time == 68.0


def test_real_readiness_caps_each_probe_to_remaining_deadline() -> None:
    """Real HTTP probes never receive a timeout beyond the total readiness budget."""
    clock = DeterministicClock()
    observed_timeouts: list[float] = []

    def fake_urlopen(_request: object, timeout: float) -> MagicMock:
        observed_timeouts.append(timeout)
        clock.current_time += 0.6
        response = MagicMock()
        response.status = 500 if len(observed_timeouts) == 1 else 200
        response.__enter__.return_value = response
        return response

    checker = RealHealthChecker(clock=clock.clock, sleeper=clock.sleep)
    with patch(
        "investment_analyst.application.local_release.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        ok, status = checker.check_health_readiness(
            port=8765,
            deadline_seconds=1.0,
            interval_seconds=0.1,
            timeout_seconds=5.0,
        )

    assert ok is False
    assert status == 200
    assert observed_timeouts[0] == pytest.approx(1.0)
    assert observed_timeouts[1] == pytest.approx(0.3)
    assert clock.current_time == pytest.approx(1.3)


def _make_readiness_test_service(
    tmp_path: Path,
    sha_new: str,
    sha_prev: str | None,
    health_checker: SimulatedHealthChecker,
    readiness_deadline: float = 15.0,
    readiness_interval: float = 0.25,
) -> tuple[LocalReleaseService, Path, Path]:
    """Set up a LocalReleaseService with staged releases for readiness tests."""
    runtime_root = tmp_path / "runtime"
    shas = [sha_new]
    if sha_prev is not None:
        shas.append(sha_prev)

    for s in shas:
        r_dir = runtime_root / "releases" / s
        (r_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (r_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (r_dir / ".venv" / "bin" / "python").touch()
        (r_dir / "scripts" / "serve_investment_analyst.py").touch()
        manifest = ReleaseManifest(
            schema_version="local-release-manifest-v1",
            commit_sha=s,
            tree_sha="b6f8115ffdddb0915cae50736dbc821c5355d3ac",
            uv_lock_sha256=hashlib.sha256(b"dummy").hexdigest(),
            python_version="Python 3.12.3",
            staged_at=datetime.now(UTC),
            release_path=str(r_dir),
        )
        (r_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    env_file = tmp_path / "config" / "service.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "ALPACA_API_KEY=k\nALPACA_API_SECRET=s\nSEC_USER_AGENT=u\n", encoding="utf-8"
    )
    env_file.chmod(0o600)

    unit_file = tmp_path / "systemd" / "investment-analyst.service"
    if sha_prev is not None:
        prev_py = runtime_root / "releases" / sha_prev / ".venv" / "bin" / "python"
        prev_scr = runtime_root / "releases" / sha_prev / "scripts" / "serve_investment_analyst.py"
        unit_content = (
            "[Unit]\nDescription=Test\n\n"
            "[Service]\n"
            f"WorkingDirectory={runtime_root / 'releases' / sha_prev}\n"
            f"EnvironmentFile={env_file}\n"
            f'ExecStart="{prev_py}" "{prev_scr}" --port 8765\n\n'
            "[Install]\nWantedBy=default.target\n"
        )
    else:
        unit_content = (
            "[Unit]\nDescription=Test\n\n"
            "[Service]\n"
            f"WorkingDirectory={runtime_root / 'releases' / sha_new}\n"
            f"EnvironmentFile={env_file}\n"
            f'ExecStart="/bin/python" "/bin/script" --port 8765\n\n'
            "[Install]\nWantedBy=default.target\n"
        )
    write_local_service_unit(unit_file, unit_content)

    # Pre-populate deployment state for prev if applicable
    if sha_prev is not None:
        state = DeploymentState(
            schema_version="local-deployment-state-v1",
            current=sha_prev,
            previous=None,
            updated_at=datetime.now(UTC),
            current_release_path=str(runtime_root / "releases" / sha_prev),
            previous_release_path=None,
        )
        runtime_root.mkdir(parents=True, exist_ok=True)
        (runtime_root / "deployment_state.json").write_text(
            state.model_dump_json(), encoding="utf-8"
        )

    systemctl = SimulatedSystemctlRunner()
    service = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl,
        health_checker=health_checker,
        systemd_unit_path=unit_file,
        service_env_path=env_file,
        readiness_deadline=readiness_deadline,
        readiness_interval=readiness_interval,
    )
    return service, unit_file, env_file


def test_readiness_transient_delay_then_success(tmp_path: Path) -> None:
    """(a) Several initial health probes fail (connection refused), then respond
    200 within the deadline → activation succeeds via multi-probe readiness polling."""
    sha_new = "3333333333333333333333333333333333333333"
    sha_prev = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"

    # 3 failed probes (connection refused), then probe 4 succeeds
    clock = DeterministicClock()
    health = SimulatedHealthChecker(
        responses=[
            (False, None),  # probe 1: connection refused
            (False, None),  # probe 2: connection refused
            (False, None),  # probe 3: connection refused
            (True, 200),  # probe 4: 200 ready
        ],
        clock=clock.clock,
        sleeper=clock.sleep,
    )
    service, _unit, _env = _make_readiness_test_service(
        tmp_path,
        sha_new,
        sha_prev,
        health,
        readiness_deadline=15.0,
        readiness_interval=0.25,
    )

    state = service.activate(sha_new)
    assert state.current == sha_new
    assert state.previous == sha_prev
    assert health.call_count == 4
    assert clock.sleeps == [0.25, 0.25, 0.25]
    assert clock.current_time == 0.75


def test_readiness_new_release_timeout_triggers_recovery_rollback(tmp_path: Path) -> None:
    """(b) The new release never becomes ready within the deadline → recovery
    rollback fires and the previous release is restored and verified."""
    sha_new = "4444444444444444444444444444444444444444"
    sha_prev = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"

    # sha_new times out across 4 probes (4 sleeps of 0.25s = 1.0s deadline)
    # then recovery rollback to sha_prev happens: probe 5 succeeds immediately
    clock = DeterministicClock()
    health = SimulatedHealthChecker(
        responses=[
            (False, None),  # sha_new probe 1 (t=0.0)
            (False, None),  # sha_new probe 2 (t=0.25)
            (False, None),  # sha_new probe 3 (t=0.50)
            (False, None),  # sha_new probe 4 (t=0.75 -> sleeps to 1.00 -> timeout)
            (True, 200),  # sha_prev rollback probe 5 -> success
        ],
        clock=clock.clock,
        sleeper=clock.sleep,
    )
    service, _unit, _env = _make_readiness_test_service(
        tmp_path,
        sha_new,
        sha_prev,
        health,
        readiness_deadline=1.0,
        readiness_interval=0.25,
    )

    with pytest.raises(ReleaseRollbackError, match="successfully rolled back and verified"):
        service.activate(sha_new)
    assert health.call_count == 5
    assert len(clock.sleeps) == 4
    assert clock.current_time == 1.0


def test_readiness_rollback_transient_delay_then_success(tmp_path: Path) -> None:
    """(c) Recovery rollback requires several probes before responding 200 → the
    rollback completes and is verified successfully after multi-probe delay."""
    sha_new = "5555555555555555555555555555555555555555"
    sha_prev = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"

    # sha_new times out across 4 probes (4 sleeps = 1.0s).
    # Then sha_prev rollback starts: fails 3 probes (3 sleeps), then probe 4 succeeds
    # (total 8 probes across both phases).
    clock = DeterministicClock()
    health = SimulatedHealthChecker(
        responses=[
            # sha_new probes:
            (False, None),  # probe 1 (t=0.0)
            (False, None),  # probe 2 (t=0.25)
            (False, None),  # probe 3 (t=0.50)
            (False, None),  # probe 4 (t=0.75 -> sleeps to 1.00 -> sha_new timeout)
            # sha_prev recovery rollback probes:
            (False, None),  # rollback probe 1 (t=1.00)
            (False, None),  # rollback probe 2 (t=1.25)
            (False, None),  # rollback probe 3 (t=1.50)
            (True, 200),  # rollback probe 4 (t=1.75 -> verified)
        ],
        clock=clock.clock,
        sleeper=clock.sleep,
    )
    service, _unit, _env = _make_readiness_test_service(
        tmp_path,
        sha_new,
        sha_prev,
        health,
        readiness_deadline=1.0,
        readiness_interval=0.25,
    )

    with pytest.raises(ReleaseRollbackError, match="successfully rolled back and verified"):
        service.activate(sha_new)
    assert health.call_count == 8
    # 4 sleeps during sha_new timeout + 3 sleeps during sha_prev rollback = 7 sleeps
    assert len(clock.sleeps) == 7
    assert clock.current_time == 1.75


def test_readiness_double_fault_critical(tmp_path: Path) -> None:
    """(d) Neither the new release nor the previous release respond within their
    deadlines across multiple probes → fail closed with CRITICAL double fault."""
    sha_new = "6666666666666666666666666666666666666666"
    sha_prev = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"

    # Both readiness polling attempts time out (4 probes each = 8 probes total)
    clock = DeterministicClock()
    health = SimulatedHealthChecker(
        responses=[(False, None)] * 8,
        clock=clock.clock,
        sleeper=clock.sleep,
    )
    service, _unit, _env = _make_readiness_test_service(
        tmp_path,
        sha_new,
        sha_prev,
        health,
        readiness_deadline=1.0,
        readiness_interval=0.25,
    )

    with pytest.raises(ReleaseRollbackError, match="CRITICAL.*ALSO FAILED"):
        service.activate(sha_new)
    assert health.call_count == 8
    assert len(clock.sleeps) == 8


def test_readiness_first_adoption_no_previous_message(tmp_path: Path) -> None:
    """A first-adoption readiness failure restores legacy without restarting it."""
    sha_new = "7777777777777777777777777777777777777777"

    clock = DeterministicClock()
    health = SimulatedHealthChecker(
        responses=[(False, None)] * 8,
        clock=clock.clock,
        sleeper=clock.sleep,
    )
    service, unit_file, _env = _make_readiness_test_service(
        tmp_path,
        sha_new,
        None,
        health,
        readiness_deadline=1.0,
        readiness_interval=0.25,
    )
    # Write a parseable unit so retarget works
    runtime_root = tmp_path / "runtime"
    new_py = runtime_root / "releases" / sha_new / ".venv" / "bin" / "python"
    new_scr = runtime_root / "releases" / sha_new / "scripts" / "serve_investment_analyst.py"
    unit_content = (
        "[Unit]\nDescription=Test\n\n"
        "[Service]\n"
        f"WorkingDirectory={runtime_root / 'releases' / sha_new}\n"
        f"EnvironmentFile={_env}\n"
        f'ExecStart="{new_py}" "{new_scr}" --port 8765\n\n'
        "[Install]\nWantedBy=default.target\n"
    )
    write_local_service_unit(unit_file, unit_content)

    legacy_before = unit_file.read_bytes()
    with pytest.raises(
        ReleaseRollbackError,
        match="candidate stopped, unmanaged legacy unit restored but not restarted",
    ):
        service.activate(sha_new)
    assert unit_file.read_bytes() == legacy_before
    assert not (tmp_path / "runtime" / "deployment_state.json").exists()
    assert isinstance(service.systemctl, SimulatedSystemctlRunner)
    assert service.systemctl.restarts == ["investment-analyst.service"]
    assert service.systemctl.stops == ["investment-analyst.service"]
    assert service.systemctl.reloaded is True


def test_first_adoption_restart_failure_does_not_restart_unmanaged_legacy(
    tmp_path: Path,
) -> None:
    """A failed first-adoption restart leaves the legacy unit inactive and unstarted."""
    sha_new = "8888888888888888888888888888888888888888"
    service, unit_file, _env = _make_readiness_test_service(
        tmp_path,
        sha_new,
        None,
        SimulatedHealthChecker(succeed=True),
        readiness_deadline=1.0,
    )
    runtime_root = tmp_path / "runtime"
    legacy_unit = (
        "[Unit]\nDescription=Legacy\n\n"
        "[Service]\n"
        f"WorkingDirectory={tmp_path / 'legacy-checkout'}\n"
        f"EnvironmentFile={_env}\n"
        f'ExecStart="{tmp_path / "legacy-checkout" / ".venv/bin/python"}" '
        f'"{tmp_path / "legacy-checkout" / "scripts/serve_investment_analyst.py"}" --port 8765\n'
    )
    write_local_service_unit(unit_file, legacy_unit)
    legacy_before = unit_file.read_bytes()
    assert isinstance(service.systemctl, SimulatedSystemctlRunner)
    service.systemctl.fail_restart = True

    with pytest.raises(
        ReleaseRollbackError,
        match="unmanaged legacy unit restored but not restarted",
    ):
        service.activate(sha_new)

    assert unit_file.read_bytes() == legacy_before
    assert service.systemctl.restarts == []
    assert service.systemctl.stops == ["investment-analyst.service"]
    assert service.systemctl.reloaded is True
    assert not (runtime_root / "deployment_state.json").exists()


def test_current_state_unit_mismatch_fails_before_retarget(tmp_path: Path) -> None:
    """A managed current release must agree with its manifest and live unit before retarget."""
    sha_current = "9999999999999999999999999999999999999999"
    sha_target = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    runtime_root = tmp_path / "runtime"
    for sha in (sha_current, sha_target):
        release_dir = runtime_root / "releases" / sha
        (release_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (release_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (release_dir / ".venv" / "bin" / "python").touch()
        (release_dir / "scripts" / "serve_investment_analyst.py").touch()
        manifest = ReleaseManifest(
            commit_sha=sha,
            tree_sha="b6f8115ffdddb0915cae50736dbc821c5355d3ac",
            uv_lock_sha256=hashlib.sha256(b"dummy").hexdigest(),
            python_version="Python 3.12.3",
            staged_at=datetime.now(UTC),
            release_path=str(release_dir),
        )
        (release_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")

    state = DeploymentState(
        current=sha_current,
        previous=None,
        updated_at=datetime.now(UTC),
        current_release_path=str(runtime_root / "releases" / sha_current),
        previous_release_path=None,
    )
    (runtime_root / "deployment_state.json").write_text(state.model_dump_json(), encoding="utf-8")
    unit_file = tmp_path / "systemd" / "investment-analyst.service"
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    unit_file.write_text(
        f"[Service]\nWorkingDirectory={runtime_root / 'releases' / sha_target}\n"
        f'ExecStart="{runtime_root / "releases" / sha_target / ".venv/bin/python"}" '
        f'"{runtime_root / "releases" / sha_target / "scripts/serve_investment_analyst.py"}"\n',
        encoding="utf-8",
    )
    before = unit_file.read_bytes()
    service = LocalReleaseService(paths=runtime_root, systemd_unit_path=unit_file)

    with pytest.raises(ReleaseRollbackError, match="state, manifest, and live unit disagree"):
        service.activate(sha_target, skip_systemd=True, skip_health_check=True)
    assert unit_file.read_bytes() == before


def test_readiness_deadline_default_and_bounds_are_fail_closed(tmp_path: Path) -> None:
    """Readiness defaults to 120 seconds and rejects values outside the CLI contract."""
    service = LocalReleaseService(paths=tmp_path)
    assert service.readiness_deadline == 120.0
    for invalid in (0.0, 600.1):
        with pytest.raises(ReleaseConfigurationError, match="between 1 and 600"):
            LocalReleaseService(paths=tmp_path, readiness_deadline=invalid)


def test_candidate_fetch_uses_only_pull_request_ref_and_verifies_tree(tmp_path: Path) -> None:
    """Candidate acquisition is exact, internally namespaced, and tree verified."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    tree = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"
    service = LocalReleaseService(
        paths=tmp_path / "runtime",
        systemd_unit_path=tmp_path / "config" / "unit",
        service_env_path=tmp_path / "config" / "env",
    )
    commands: list[list[str]] = []

    def run(command: list[str], *args: object, **kwargs: object) -> MagicMock:
        commands.append(command)
        if "show-ref" in command:
            return MagicMock(returncode=1, stdout="")
        if "^{commit}" in " ".join(command):
            return MagicMock(returncode=0, stdout=f"{sha}\n")
        if "^{tree}" in " ".join(command):
            return MagicMock(returncode=0, stdout=f"{tree}\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch.object(service, "_query_remote_ref", side_effect=[sha, sha]),
        patch("subprocess.run", side_effect=run),
    ):
        assert service.fetch_origin_candidate("59", sha) == (sha, tree)

    fetch_commands = [command for command in commands if "fetch" in command]
    assert len(fetch_commands) == 1
    assert "refs/pull/59/head" in " ".join(fetch_commands[0])
    fetch_text = " ".join(fetch_commands[0])
    assert "refs/heads/main" not in fetch_text
    assert "refs/investment-analyst/candidates/59/head" in fetch_text


def test_candidate_fetch_race_restores_previous_internal_ref(tmp_path: Path) -> None:
    """A moving PR head fails closed and restores the prior candidate mirror ref."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    moved = "2222222222222222222222222222222222222222"
    service = LocalReleaseService(
        paths=tmp_path / "runtime",
        systemd_unit_path=tmp_path / "config" / "unit",
        service_env_path=tmp_path / "config" / "env",
    )
    commands: list[list[str]] = []

    def run(command: list[str], *args: object, **kwargs: object) -> MagicMock:
        commands.append(command)
        if "show-ref" in command:
            return MagicMock(returncode=0, stdout=f"{sha}\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch.object(service, "_query_remote_ref", side_effect=[sha, moved]),
        patch("subprocess.run", side_effect=run),
        pytest.raises(ReleaseAcquisitionError, match="moved during acquisition"),
    ):
        service.fetch_origin_candidate(59, sha)

    rollback_commands = [command for command in commands if "update-ref" in command]
    assert len(rollback_commands) == 1
    assert rollback_commands[0][-2:] == [
        "refs/investment-analyst/candidates/59/head",
        sha,
    ]


def test_candidate_input_rejects_non_positive_pr_and_short_sha(tmp_path: Path) -> None:
    """Candidate acquisition never turns untrusted CLI text into a remote ref."""
    service = LocalReleaseService(
        paths=tmp_path / "runtime",
        systemd_unit_path=tmp_path / "config" / "unit",
        service_env_path=tmp_path / "config" / "env",
    )
    with pytest.raises(ReleaseAcquisitionError, match="positive integer"):
        service.fetch_origin_candidate("0", "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb")
    with pytest.raises(ReleaseAcquisitionError, match="full 40-character"):
        service.fetch_origin_candidate("59", "1b0a1ab")


def test_candidate_update_reuses_verified_activation_rollback_path(tmp_path: Path) -> None:
    """Candidate update stages the exact PR then delegates activation recovery unchanged."""
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    state = DeploymentState(
        current=sha,
        previous="2222222222222222222222222222222222222222",
        updated_at=datetime.now(UTC),
        current_release_path=str(tmp_path / "runtime" / "releases" / sha),
        previous_release_path=str(tmp_path / "runtime" / "releases" / "previous"),
    )
    service = LocalReleaseService(paths=tmp_path / "runtime")
    with (
        patch.object(service, "stage_candidate", return_value=MagicMock(commit_sha=sha)) as stage,
        patch.object(service, "activate", return_value=state) as activate,
    ):
        assert service.candidate_update(59, sha, skip_systemd=True, skip_health_check=True) == state

    stage.assert_called_once_with(59, sha)
    assert activate.call_args.kwargs["sha"] == sha
    assert activate.call_args.kwargs["skip_systemd"] is True
    assert activate.call_args.kwargs["skip_health_check"] is True
