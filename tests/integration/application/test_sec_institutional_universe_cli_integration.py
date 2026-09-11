"""Integration tests for the two manager-universe CLI wrappers with a live runtime contract.

The observed defect was ``WorkspaceService.__init__() got an unexpected keyword argument 'root'``
followed by the nonexistent ``StorageLocationRequest(workspace_root=...)`` field. Both wrappers must
keep their public flags and JSON output while using the live runtime and location contract.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.workspace.service import WorkspaceNotInitializedError

_ZIP_URL = (
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    "01mar2026-31may2026_form13f.zip"
)
_USER_AGENT = "Analyst user@example.com"


def _universe_zip() -> bytes:
    submissions = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "0001067983-26-000010\t15-APR-2026\t13F-HR\t0001067983\t31-MAR-2026\n"
        "0000000002-26-000001\t10-MAY-2026\t13F-HR\t0000000002\t31-MAR-2026\n"
    )
    coverpage = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT\n"
        "0001067983-26-000010\tBERKSHIRE HATHAWAY INC\tN\n"
        "0000000002-26-000001\tALPHA ASSET MANAGEMENT\tN\n"
    )
    infotable = (
        "ACCESSION_NUMBER\tCUSIP\tVALUE\n"
        "0001067983-26-000010\t037833100\t80000\n"
        "0000000002-26-000001\t037833100\t50000\n"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SUBMISSION.tsv", submissions)
        archive.writestr("COVERPAGE.tsv", coverpage)
        archive.writestr("INFOTABLE.tsv", infotable)
    return stream.getvalue()


@pytest.fixture
def sec_http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Serve the official catalog and dataset over the real transport class without network."""
    seen: list[str] = []
    payload = _universe_zip()

    def fake_get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        seen.append(url)
        assert headers.get("User-Agent") == _USER_AGENT
        if url == SEC_13F_DATA_SETS_CATALOG_URL:
            body = f'<html><a href="{_ZIP_URL}">2026 Q1</a></html>'.encode()
        elif url == _ZIP_URL:
            body = payload
        else:
            raise AssertionError(f"unexpected SEC request: {url}")
        return HttpResponse(status_code=200, body=body, headers={}, url=url)

    monkeypatch.setattr(UrlLibHttpTransport, "get", fake_get)
    monkeypatch.setenv("SEC_USER_AGENT", _USER_AGENT)
    return SimpleNamespace(seen=seen)


def _initialized_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    ApplicationRuntime.create_default().workspace_service.initialize(root)
    return root


def test_refresh_and_query_wrappers_run_against_a_temporary_workspace(
    tmp_path: Path, sec_http: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import query_sec_institutional_manager_universe as query_module
    from scripts import refresh_sec_institutional_manager_universe as refresh_module

    workspace = _initialized_workspace(tmp_path)

    assert refresh_module.main(["--workspace", str(workspace)]) == 0
    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["status"] == "PASS"
    assert refreshed["candidate_manager_count"] == 2
    assert refreshed["selected_manager_count"] == 2
    assert refreshed["matched_asset_count"] == 1
    assert refreshed["created"] is True
    assert sec_http.seen == [SEC_13F_DATA_SETS_CATALOG_URL, _ZIP_URL]

    available_at = datetime.fromisoformat(refreshed["retrieved_at"])
    assert available_at.tzinfo is not None
    assert (
        query_module.main(
            [
                "--workspace",
                str(workspace),
                "--known-at",
                refreshed["retrieved_at"],
                "--asset-id",
                "equity:us:aapl",
            ]
        )
        == 0
    )
    queried = json.loads(capsys.readouterr().out)
    assert queried["status"] == "PASS"
    assert queried["snapshot_id"] == refreshed["snapshot_id"]
    assert queried["filtered_candidate_count"] == 2
    assert {item["manager_cik"] for item in queried["candidates"]} == {
        "0001067983",
        "0000000002",
    }
    assert queried["candidates"][0]["cusip"] == "037833100"
    assert queried["candidates"][0]["report_period"] == date(2026, 3, 31).isoformat()
    assert queried["covered_cusips"] == ["037833100"]


def test_query_wrapper_fails_closed_on_an_uninitialized_workspace(
    tmp_path: Path, sec_http: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import query_sec_institutional_manager_universe as query_module

    del sec_http
    uninitialized = tmp_path / "missing"
    uninitialized.mkdir()
    with pytest.raises(WorkspaceNotInitializedError, match="manifest was not found"):
        query_module.main(
            [
                "--workspace",
                str(uninitialized),
                "--known-at",
                "2026-06-02T00:00:00+00:00",
            ]
        )
    assert capsys.readouterr().out == ""
