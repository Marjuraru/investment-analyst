"""Integration tests for deploy_local_release CLI utility."""

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from scripts.deploy_local_release import main

from investment_analyst.application.local_service_unit import (
    AaplLocalServiceUnitConfig,
    render_local_service_unit,
    write_local_service_unit,
)


def _create_mock_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in (
            ("pyproject.toml", b'[project]\nname="investment-analyst"\n'),
            ("uv.lock", b"version = 1\n"),
            ("scripts/serve_investment_analyst.py", b"# server\n"),
            ("src/investment_analyst/__init__.py", b"# init\n"),
        ):
            data = content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_cli_full_lifecycle_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test full sequential CLI flow: adopt-env -> stage -> activate -> status -> update."""
    sha1 = "1b0a1ab98d7ddbd0b202f40bb8c066a48c907cbb"
    sha2 = "2222222222222222222222222222222222222222"
    tree_sha = "b6f8115ffdddb0915cae50736dbc821c5355d3ac"

    runtime_root = tmp_path / "runtime"
    config_dir = tmp_path / "config"
    env_file = config_dir / "service.env"
    unit_file = tmp_path / "systemd" / "investment-analyst.service"

    # 1. Setup source env file
    source_env = tmp_path / "source.env"
    source_env.write_text(
        "ALPACA_API_KEY=test_key\n"
        "ALPACA_API_SECRET=test_sec\n"
        "SEC_USER_AGENT=TestUser user@test.com\n",
        encoding="utf-8",
    )
    source_env.chmod(0o600)

    # 2. Setup initial unit
    unit_config = AaplLocalServiceUnitConfig(
        repository_root=tmp_path / "old_repo",
        environment_file=tmp_path / "old_repo" / ".env",
        workspace_root=tmp_path / "workspace",
        port=8765,
        schedule=None,
    )
    write_local_service_unit(unit_file, render_local_service_unit(unit_config))

    archive_bytes = _create_mock_archive()

    def mock_subprocess(cmd, *args, **kwargs):
        if "archive" in cmd:
            return MagicMock(returncode=0, stdout=archive_bytes)
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

    with (
        patch(
            "investment_analyst.application.local_release.LocalReleaseService.fetch_origin_main",
            side_effect=lambda s=None: (s or sha1, tree_sha),
        ),
        patch(
            "investment_analyst.application.local_release.LocalReleaseService.ensure_uv",
            return_value=Path("/bin/uv"),
        ),
        patch("subprocess.run", side_effect=mock_subprocess),
    ):
        # Step A: adopt-env
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "--env-file",
                str(env_file),
                "adopt-env",
                "--source",
                str(source_env),
            ]
        )
        assert code == 0
        assert env_file.is_file()

        # Step B: stage sha1
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "stage",
                "--sha",
                sha1,
            ]
        )
        assert code == 0
        assert (runtime_root / "releases" / sha1 / "manifest.json").is_file()

        # Step C: activate sha1
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "--unit-file",
                str(unit_file),
                "--env-file",
                str(env_file),
                "activate",
                "--sha",
                sha1,
                "--skip-systemd",
                "--skip-health-check",
            ]
        )
        assert code == 0
        current_unit = unit_file.read_text(encoding="utf-8")
        assert f"releases/{sha1}" in current_unit

        # Step D: status (JSON)
        capsys.readouterr()
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "--unit-file",
                str(unit_file),
                "status",
                "--no-systemd",
                "--no-http",
                "--json",
            ]
        )
        assert code == 0
        out, _ = capsys.readouterr()
        status_json = json.loads(out)
        assert status_json["current_commit"] == sha1
        assert status_json["unit_matches_current"] is True

        # Step E: update to sha2
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "--unit-file",
                str(unit_file),
                "--env-file",
                str(env_file),
                "update",
                "--sha",
                sha2,
                "--skip-systemd",
                "--skip-health-check",
            ]
        )
        assert code == 0
        unit_after_update = unit_file.read_text(encoding="utf-8")
        assert f"releases/{sha2}" in unit_after_update

        # Step F: rollback
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "--unit-file",
                str(unit_file),
                "--env-file",
                str(env_file),
                "rollback",
                "--skip-systemd",
                "--skip-health-check",
            ]
        )
        assert code == 0
        unit_after_rb = unit_file.read_text(encoding="utf-8")
        assert f"releases/{sha1}" in unit_after_rb

        # Step G: idempotence on repeated stage/activate
        code = main(
            [
                "--runtime-root",
                str(runtime_root),
                "--unit-file",
                str(unit_file),
                "--env-file",
                str(env_file),
                "activate",
                "--sha",
                sha1,
                "--skip-systemd",
                "--skip-health-check",
            ]
        )
        assert code == 0


def test_cli_error_handling(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test CLI error handling and fail-closed exit codes."""
    runtime_root = tmp_path / "runtime"

    code = main(
        [
            "--runtime-root",
            str(runtime_root),
            "stage",
            "--sha",
            "not-a-valid-sha",
        ]
    )
    assert code == 1
    _, err = capsys.readouterr()
    assert "40 lowercase hexadecimal" in err

    code = main(
        [
            "--runtime-root",
            str(runtime_root),
            "rollback",
            "--skip-systemd",
            "--skip-health-check",
        ]
    )
    assert code == 1
    _, err = capsys.readouterr()
    assert "No previous deployment" in err
