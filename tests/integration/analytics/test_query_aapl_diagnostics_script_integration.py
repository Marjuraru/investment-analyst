"""Subprocess tests for the compact consolidated diagnostic CLI."""

import json
import subprocess
import sys
from pathlib import Path

from investment_analyst.storage import LocalStorage, StoragePaths


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
    with LocalStorage(StoragePaths.from_root(tmp_path)):
        pass
    result = _run(tmp_path)
    explicit_legacy = _run(tmp_path, "--format", "json")
    assert result.returncode == 0
    assert explicit_legacy.returncode == 0
    assert result.stdout == explicit_legacy.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "unavailable"
    assert payload["market"]["status"] == "not_found"
    assert payload["fundamental"]["status"] == "not_found"
    assert "combined_score" not in result.stdout
    assert "Traceback" not in result.stderr


def test_script_supports_versioned_json_and_human_readable_text(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)):
        pass

    enriched = _run(tmp_path, "--format", "daily-json")

    assert enriched.returncode == 0
    payload = json.loads(enriched.stdout)
    assert payload["schema_version"] == "aapl-daily-diagnostic-report-v1"
    assert payload["status"] == "unavailable"
    assert payload["market"]["metrics"] == []
    assert payload["market"]["freshness"] == {
        "availability_age_days": None,
        "reference_age_days": None,
    }
    assert payload["traceability"]["verified"] is True
    assert "combined_score" not in payload

    text = _run(tmp_path, "--format", "text")
    assert text.returncode == 0
    assert "APPLE (AAPL) DAILY DIAGNOSTIC REPORT" in text.stdout
    assert "Availability: unavailable" in text.stdout
    assert "MARKET DIAGNOSTIC\nStatus: not_found" in text.stdout
    assert "FUNDAMENTAL DIAGNOSTIC\nStatus: not_found" in text.stdout
    assert "IEX is single-exchange coverage" in text.stdout
    assert "Traceback" not in text.stderr


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

    invalid_format = _run(tmp_path, "--format", "yaml")
    assert invalid_format.returncode != 0
    assert "invalid choice" in invalid_format.stderr
