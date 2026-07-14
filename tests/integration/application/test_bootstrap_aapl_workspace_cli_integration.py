"""Subprocess coverage for the persistent Apple workspace bootstrap command."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

FIXTURE_DIR = Path("tests/fixtures/sec")


def _load(name: str) -> dict[str, object]:
    return json.loads(
        (FIXTURE_DIR / name).read_text(encoding="utf-8"),
        parse_int=str,
        parse_float=str,
    )


def _sec_documents() -> tuple[bytes, bytes]:
    submissions = _load("aapl_submissions.json")
    recent = submissions["filings"]["recent"]
    recent["acceptanceDateTime"] = [
        "2026-01-30T21:00:00Z",
        "2026-04-30T21:00:00Z",
    ]
    recent["primaryDocument"] = [
        "aapl-20251231x10k.htm",
        "aapl-20260331x10q.htm",
    ]

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
        result = duration(annual, value)
        del result["start"]
        return result

    companyfacts = _load("aapl_companyfacts.json")
    companyfacts["facts"] = {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [duration(True, "1000"), duration(False, "260")]}
            },
            "NetIncomeLoss": {"units": {"USD": [duration(True, "200"), duration(False, "52")]}},
            "Assets": {"units": {"USD": [instant(True, "5000"), instant(False, "5100")]}},
            "Liabilities": {"units": {"USD": [instant(True, "3000"), instant(False, "3050")]}},
            "StockholdersEquity": {
                "units": {"USD": [instant(True, "2000"), instant(False, "2050")]}
            },
        },
        "dei": companyfacts["facts"]["dei"],
    }
    return (
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    )


def _bars() -> bytes:
    bars = []
    for offset in range(25):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset)
        value = 100 + offset
        bars.append(
            {
                "t": timestamp.isoformat().replace("+00:00", "Z"),
                "o": value,
                "h": value + 3,
                "l": value - 1,
                "c": value + 2,
                "v": 1_000_000 + offset * 10_000,
                "n": 10_000 + offset,
                "vw": value + 1,
            }
        )
    return json.dumps(
        {"bars": bars, "symbol": "AAPL", "next_page_token": None},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _run(
    workspace: Path,
    submissions: Path,
    companyfacts: Path,
    bars: Path,
    *extra: str,
    credentials: bool = True,
) -> subprocess.CompletedProcess[str]:
    launcher = "\n".join(
        (
            "import runpy, sys",
            "from pathlib import Path",
            "from investment_analyst.providers.http import HttpResponse",
            "import investment_analyst.providers.http as http_module",
            "class FakeTransport:",
            "    def __init__(self, *args, **kwargs): pass",
            "    def get(self, url, *, headers, timeout_seconds):",
            "        if '/submissions/' in url:",
            f"            body = Path({str(submissions)!r}).read_bytes()",
            "        elif '/companyfacts/' in url:",
            f"            body = Path({str(companyfacts)!r}).read_bytes()",
            "        else:",
            f"            body = Path({str(bars)!r}).read_bytes()",
            "        return HttpResponse(status_code=200, body=body, headers={}, url=url)",
            "http_module.UrlLibHttpTransport = FakeTransport",
            "sys.argv = ['bootstrap_aapl_workspace.py', *sys.argv[1:]]",
            "runpy.run_path('scripts/bootstrap_aapl_workspace.py', run_name='__main__')",
        )
    )
    environment = os.environ.copy()
    environment["INVESTMENT_ANALYST_WORKSPACE"] = str(workspace)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path("src").resolve()), str(Path.cwd()), environment.get("PYTHONPATH", "")]
    )
    if credentials:
        environment.update(
            {
                "ALPACA_API_KEY": "cli-key",
                "ALPACA_API_SECRET": "cli-secret",
                "SEC_USER_AGENT": "Investment Analyst cli@example.com",
            }
        )
    else:
        environment.pop("ALPACA_API_KEY", None)
        environment.pop("ALPACA_API_SECRET", None)
        environment.pop("SEC_USER_AGENT", None)
    arguments = [
        "--market-start",
        "2026-01-01",
        "--market-end",
        "2026-01-26",
        "--fundamental-frequency",
        "quarterly",
    ]
    if "--known-at" not in extra:
        arguments.extend(("--known-at", "2026-07-20T00:00:00Z"))
    arguments.extend(extra)
    return subprocess.run(
        [sys.executable, "-c", launcher, *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _fixture_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    submissions_body, companyfacts_body = _sec_documents()
    submissions = tmp_path / "submissions.json"
    companyfacts = tmp_path / "companyfacts.json"
    bars = tmp_path / "bars.json"
    submissions.write_bytes(submissions_body)
    companyfacts.write_bytes(companyfacts_body)
    bars.write_bytes(_bars())
    return submissions, companyfacts, bars


def test_cli_bootstraps_default_workspace_and_reuses_compact_output(tmp_path) -> None:
    workspace = tmp_path / "persistent-workspace"
    submissions, companyfacts, bars = _fixture_files(tmp_path)

    first = _run(workspace, submissions, companyfacts, bars)
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout)
    assert payload["summary"]["overall_status"] == "complete"
    assert payload["workspace"]["root"] == str(workspace)
    assert payload["source"]["feed"] == "iex"
    assert payload["consolidated"]["status"] == "complete"
    assert set(payload) == {
        "notice",
        "workspace",
        "request",
        "effective_known_at",
        "source",
        "stages",
        "consolidated",
        "summary",
    }
    assert "combined_score" not in first.stdout
    assert "record_key" not in first.stdout
    assert "cli-key" not in first.stdout + first.stderr
    assert "cli-secret" not in first.stdout + first.stderr
    assert "Traceback" not in first.stderr

    second = _run(workspace, submissions, companyfacts, bars)
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["summary"]["counts"]["raw_records_created"] == 0
    assert second_payload["summary"]["counts"]["observations_created"] == 0
    assert second_payload["summary"]["counts"]["metric_results_created"] == 0
    assert second_payload["summary"]["counts"]["diagnostics_created"] == 0


def test_cli_early_known_at_fails_without_success_json_or_traceback(tmp_path) -> None:
    workspace = tmp_path / "too-early"
    submissions, companyfacts, bars = _fixture_files(tmp_path)
    result = _run(
        workspace,
        submissions,
        companyfacts,
        bars,
        "--known-at",
        "2000-01-01T00:00:00Z",
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "minimum_known_at" in result.stderr
    assert "Traceback" not in result.stderr
    assert "cli-key" not in result.stderr
    assert "cli-secret" not in result.stderr


def test_cli_requires_credentials_before_initializing_workspace(tmp_path) -> None:
    workspace = tmp_path / "missing-credentials"
    submissions, companyfacts, bars = _fixture_files(tmp_path)
    result = _run(
        workspace,
        submissions,
        companyfacts,
        bars,
        credentials=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "required" in result.stderr
    assert not workspace.exists()
    assert "Traceback" not in result.stderr
