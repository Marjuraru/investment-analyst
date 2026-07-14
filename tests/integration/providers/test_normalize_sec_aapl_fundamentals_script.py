"""Subprocess tests for the offline SEC fundamental normalization command."""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarClient, SecEdgarIdentity
from investment_analyst.providers.fundamentals.sec_raw_records import sec_document_to_raw_record
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

FIXTURE_DIR = Path("tests/fixtures/sec")
SCRIPT = Path("scripts/normalize_sec_aapl_fundamentals.py")


class FixtureTransport:
    """Offline transport returning two prepared documents."""

    def __init__(self, bodies: list[bytes]) -> None:
        self._bodies = bodies

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        return HttpResponse(200, self._bodies.pop(0), {}, url)


def _fixture(name: str) -> dict[str, object]:
    return json.loads(
        (FIXTURE_DIR / name).read_text(),
        parse_int=str,
        parse_float=str,
    )


def _prepare_storage(root: Path) -> None:
    submissions = _fixture("aapl_submissions.json")
    recent = submissions["filings"]["recent"]
    recent["acceptanceDateTime"] = [
        "2026-01-30T21:00:00Z",
        "2026-04-30T21:00:00Z",
    ]
    recent["primaryDocument"] = ["annual.htm", "quarterly.htm"]

    def duration(annual: bool, value: str) -> dict[str, str]:
        return {
            "start": "2025-01-01" if annual else "2026-01-01",
            "end": "2025-12-31" if annual else "2026-03-31",
            "val": value,
            "accn": "0000320193-26-000001" if annual else "0000320193-26-000002",
            "fy": "2025" if annual else "2026",
            "fp": "FY" if annual else "Q1",
            "form": "10-K" if annual else "10-Q",
            "filed": "2026-01-30" if annual else "2026-04-30",
        }

    def instant(annual: bool, value: str) -> dict[str, str]:
        item = duration(annual, value)
        del item["start"]
        return item

    companyfacts = _fixture("aapl_companyfacts.json")
    companyfacts["facts"]["us-gaap"] = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": [duration(True, "1000"), duration(False, "250")]}
        },
        "NetIncomeLoss": {"units": {"USD": [duration(True, "200"), duration(False, "50")]}},
        "Assets": {"units": {"USD": [instant(True, "5000"), instant(False, "5100")]}},
        "Liabilities": {"units": {"USD": [instant(True, "3000"), instant(False, "3050")]}},
        "StockholdersEquity": {"units": {"USD": [instant(True, "2000"), instant(False, "2050")]}},
    }
    bodies = [
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    ]
    client = SecEdgarClient(
        FixtureTransport(bodies),
        SecEdgarIdentity("Investment Analyst script-test@example.com"),
        sleep=lambda _: None,
        clock=lambda: datetime(2026, 7, 13, 18, tzinfo=UTC),
    )
    with LocalStorage(StoragePaths.from_root(root)) as storage:
        for document in client.fetch_aapl_issuer_documents().documents:
            storage.raw_records.save(sec_document_to_raw_record(document))


def _run(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_reports_missing_snapshots_without_traceback(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    result = _run(tmp_path, env)

    assert result.returncode != 0
    assert "no compatible local snapshot" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_script_outputs_json_and_reuses_observations_without_credentials(tmp_path: Path) -> None:
    _prepare_storage(tmp_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    env["SEC_USER_AGENT"] = "must-not-appear@example.com"

    first = _run(tmp_path, env)
    second = _run(tmp_path, env)
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)

    assert first.returncode == 0
    assert second.returncode == 0
    assert first_payload["summary"]["observations_created"] == 10
    assert second_payload["summary"]["observations_created"] == 0
    assert second_payload["summary"]["observations_reused"] == 10
    output = first.stdout + first.stderr + second.stdout + second.stderr
    assert "must-not-appear@example.com" not in output
    assert "company_facts" not in output
    assert "observation_ids" not in output
    assert "Traceback" not in output
