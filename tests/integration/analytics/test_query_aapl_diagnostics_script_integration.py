"""Subprocess tests for the compact consolidated diagnostic CLI."""

import json
import subprocess
import sys
from pathlib import Path


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "scripts/query_aapl_diagnostics.py",
            "--root",
            str(root),
            "--known-at",
            "2026-07-14T04:41:55Z",
            "--fundamental-frequency",
            "quarterly",
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_script_returns_valid_unavailable_json_without_writes(tmp_path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "unavailable"
    assert payload["market"]["status"] == "not_found"
    assert payload["fundamental"]["status"] == "not_found"
    assert "combined_score" not in result.stdout
    assert "Traceback" not in result.stderr


def test_script_rejects_naive_known_at_and_invalid_frequency(tmp_path) -> None:
    naive = subprocess.run(
        [
            sys.executable,
            "scripts/query_aapl_diagnostics.py",
            "--root",
            str(tmp_path),
            "--known-at",
            "2026-07-14T04:41:55",
            "--fundamental-frequency",
            "quarterly",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert naive.returncode != 0
    assert "timezone" in naive.stderr

    invalid = subprocess.run(
        [
            sys.executable,
            "scripts/query_aapl_diagnostics.py",
            "--root",
            str(tmp_path),
            "--known-at",
            "2026-07-14T04:41:55Z",
            "--fundamental-frequency",
            "monthly",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode != 0
    assert "annual or quarterly" in invalid.stderr
