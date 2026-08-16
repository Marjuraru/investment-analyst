"""Unit tests for independent local release runtime, acquisition, and management."""

import hashlib
import io
import stat
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from investment_analyst.application.aapl_bootstrap_models import AaplRefreshMode
from investment_analyst.application.aapl_scheduler import AaplDailyScheduleConfig
from investment_analyst.application.local_release import (
    DeploymentState,
    LocalReleaseService,
    ReleaseAcquisitionError,
    ReleaseConfigurationError,
    ReleaseManifest,
    ReleaseRollbackError,
    ReleaseUnitError,
    SimulatedHealthChecker,
    SimulatedSystemctlRunner,
    _safe_tar_extract,
)
from investment_analyst.application.local_service_unit import (
    AaplLocalServiceUnitConfig,
    render_local_service_unit,
    write_local_service_unit,
)
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


def test_activation_and_automatic_rollback_on_health_failure(tmp_path: Path) -> None:
    """Test activation rollback when service restart or health verification fails."""
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

    state1 = service.activate(sha1)
    assert state1.current == sha1
    assert state1.previous is None

    health_checker.succeed = False
    with pytest.raises(ReleaseRollbackError, match="Health check failed"):
        service.activate(sha2)

    current_unit = unit_file.read_text(encoding="utf-8")
    assert f"releases/{sha1}" in current_unit
    assert f"releases/{sha2}" not in current_unit

    health_checker.succeed = True
    state2 = service.activate(sha2)
    assert state2.current == sha2
    assert state2.previous == sha1

    state_rb = service.rollback()
    assert state_rb.current == sha1
    current_unit_rb = unit_file.read_text(encoding="utf-8")
    assert f"releases/{sha1}" in current_unit_rb


def test_status_report(tmp_path: Path) -> None:
    """Test generating release status report."""
    runtime_root = tmp_path / "runtime"
    sha = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    r_dir = runtime_root / "releases" / sha
    r_dir.mkdir(parents=True, exist_ok=True)
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

    state = DeploymentState(
        schema_version="local-deployment-state-v1",
        current=sha,
        previous=None,
        updated_at=datetime.now(UTC),
        current_release_path=str(r_dir),
        previous_release_path=None,
    )
    (runtime_root / "deployment_state.json").write_text(state.model_dump_json(), encoding="utf-8")

    unit_file = tmp_path / "unit.service"
    unit_file.write_text(
        f"[Service]\n"
        f"WorkingDirectory={r_dir}\n"
        f"ExecStart=/bin/py /bin/scr\n"
        f"EnvironmentFile=/etc/env\n",
        encoding="utf-8",
    )

    systemctl = SimulatedSystemctlRunner(is_active_result=True, is_enabled_result=True)
    health = SimulatedHealthChecker(succeed=True, status_code=200)

    service = LocalReleaseService(
        paths=runtime_root,
        systemctl=systemctl,
        health_checker=health,
        systemd_unit_path=unit_file,
    )

    report = service.status()
    assert report.current_commit == sha
    assert report.unit_matches_current is True
    assert report.service_active is True
    assert report.overview_status == 200
