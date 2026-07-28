"""Boundary tests for the catalog-backed SEC refresh command."""

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[3]
_SCRIPT = _ROOT / "scripts" / "refresh_sec_fundamentals.py"


def test_cli_requires_sec_identity_before_opening_storage(tmp_path: Path) -> None:
    storage = tmp_path / "not-created"
    environment = dict(os.environ)
    environment.pop("SEC_USER_AGENT", None)

    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(storage),
            "--asset-id",
            "equity:us:amd",
            "--frequency",
            "annual",
        ],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == ("SEC_USER_AGENT is required and was not provided.")
    assert not storage.exists()


def test_cli_rejects_invalid_frequency_without_provider_access(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(tmp_path / "not-created"),
            "--asset-id",
            "equity:us:amd",
            "--frequency",
            "daily",
        ],
        cwd=_ROOT,
        env={**os.environ, "SEC_USER_AGENT": "Investment Analyst test@example.com"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "frequency must be annual or quarterly" in completed.stderr
