"""CLI contract tests without provider or permanent-workspace access."""

import json
import runpy
import sys
from pathlib import Path

import pytest

from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.providers.crypto.deribit import DeribitError

_ROOT = Path(__file__).parents[3]
_REFRESH = _ROOT / "scripts" / "refresh_crypto_derivatives.py"
_QUERY = _ROOT / "scripts" / "query_crypto_derivatives.py"


class _JsonResult:
    def __init__(self, document: dict[str, object]) -> None:
        self._document = document

    def to_json_dict(self) -> dict[str, object]:
        return self._document


class _FakeApplication:
    def __init__(self) -> None:
        self.refresh_request = None
        self.query_request = None

    def refresh_crypto_derivatives(self, request, *, location):
        self.refresh_request = (request, location)
        return _JsonResult(
            {
                "schema_version": "crypto-derivatives-refresh-summary-v1",
                "asset_id": request.asset_id,
                "traceability_verified": True,
            }
        )

    def query_crypto_derivatives(self, request, *, location):
        self.query_request = (request, location)
        return _JsonResult(
            {
                "schema_version": "crypto-derivatives-query-result-v1",
                "asset_id": request.asset_id,
                "traceability_verified": True,
            }
        )


def _run(script: Path, arguments: list[str]) -> int:
    previous = sys.argv
    sys.argv = [str(script), *arguments]
    try:
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = previous
    assert isinstance(stopped.value.code, int)
    return stopped.value.code


def test_refresh_and_query_cli_delegate_compact_versioned_contracts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    application = _FakeApplication()
    monkeypatch.setattr(
        InvestmentAnalystApplication,
        "create_default",
        classmethod(lambda cls: application),
    )
    shared = [
        "--root",
        str(tmp_path / "legacy"),
        "--asset-id",
        "crypto:btc-usd",
        "--start",
        "2026-08-01",
        "--end",
        "2026-08-07",
    ]

    assert _run(_REFRESH, [*shared, "--known-at", "2026-08-11T00:00:00Z"]) == 0
    refresh_output = json.loads(capsys.readouterr().out)
    assert refresh_output["summary"]["schema_version"] == ("crypto-derivatives-refresh-summary-v1")
    assert application.refresh_request[0].asset_id == "crypto:btc-usd"

    assert _run(_QUERY, [*shared, "--known-at", "2026-08-11T00:00:00Z"]) == 0
    query_output = json.loads(capsys.readouterr().out)
    assert query_output["result"]["schema_version"] == "crypto-derivatives-query-result-v1"
    assert application.query_request[0].known_at.isoformat() == "2026-08-11T00:00:00+00:00"
    assert "raw_payload" not in json.dumps((refresh_output, query_output))
    assert "recommendation" not in json.dumps((refresh_output, query_output)).casefold()


def test_refresh_cli_reports_contract_failure_without_traceback_or_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class _FailingApplication(_FakeApplication):
        def refresh_crypto_derivatives(self, request, *, location):
            del request, location
            raise DeribitError("simulated compact contract failure")

    monkeypatch.setattr(
        InvestmentAnalystApplication,
        "create_default",
        classmethod(lambda cls: _FailingApplication()),
    )

    exit_code = _run(
        _REFRESH,
        [
            "--root",
            str(tmp_path / "unused"),
            "--asset-id",
            "crypto:eth-usd",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-07",
        ],
    )
    output = capsys.readouterr()

    assert exit_code == 1
    assert output.out == ""
    assert output.err.strip() == (
        "Crypto derivatives refresh failed: simulated compact contract failure"
    )
    assert "Traceback" not in output.err
