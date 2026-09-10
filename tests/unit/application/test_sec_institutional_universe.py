"""Unit tests for SEC Form 13F manager universe application service."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
    SecInstitutionalUniverseApplicationError,
    resolve_catalog_sec_cusip_mappings,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseQueryRequest,
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)


def _build_test_zip() -> bytes:
    submissions_tsv = (
        "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
        "0001067983-26-000010\t15-APR-2026\t13F-HR\t0001067983\t31-MAR-2026\n"
        "0000000002-26-000001\t10-MAY-2026\t13F-HR\t0000000002\t31-MAR-2026\n"
        "0000000003-26-000001\t10-MAY-2026\t13F-HR\t0000000003\t31-MAR-2026\n"
    )
    coverpage_tsv = (
        "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT\n"
        "0001067983-26-000010\tBERKSHIRE HATHAWAY INC\tN\n"
        "0000000002-26-000001\tALPHA ASSET MANAGEMENT\tN\n"
        "0000000003-26-000001\tBETA PARTNERS LP\tN\n"
    )
    infotable_tsv = (
        "ACCESSION_NUMBER\tCUSIP\tVALUE\n"
        "0001067983-26-000010\t037833100\t80000\n"
        "0000000002-26-000001\t037833100\t50000\n"
        "0000000003-26-000001\t037833100\t10000\n"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("SUBMISSION.tsv", submissions_tsv)
        zf.writestr("COVERPAGE.tsv", coverpage_tsv)
        zf.writestr("INFOTABLE.tsv", infotable_tsv)
    return stream.getvalue()


class _MockTransport(HttpTransport):
    def __init__(self, catalog_html: str, zip_bytes: bytes, zip_url: str) -> None:
        self.catalog_html = catalog_html
        self.zip_bytes = zip_bytes
        self.zip_url = zip_url
        self.call_count = 0

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.call_count += 1
        if url == SEC_13F_DATA_SETS_CATALOG_URL:
            return HttpResponse(
                status_code=200,
                body=self.catalog_html.encode("utf-8"),
                headers={"Content-Type": "text/html; charset=utf-8"},
                url=url,
            )
        if url == self.zip_url:
            return HttpResponse(
                status_code=200,
                body=self.zip_bytes,
                headers={"Content-Type": "application/zip"},
                url=url,
            )
        raise RuntimeError(f"Unexpected URL request: {url}")


def test_resolve_catalog_sec_cusip_mappings() -> None:
    runtime = ApplicationRuntime.create_default()
    mappings = resolve_catalog_sec_cusip_mappings(runtime.catalog)
    # Apple AAPL is in the catalog
    assert "037833100" in mappings
    assert mappings["037833100"] == "equity:us:aapl"


def test_application_refresh_and_query_lifecycle() -> None:
    zip_url = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip"
    catalog_html = f'<html><a href="{zip_url}">2026 Q1</a></html>'
    zip_bytes = _build_test_zip()

    transport = _MockTransport(catalog_html, zip_bytes, zip_url)
    sec_identity = SecEdgarIdentity("Analyst user@example.com")

    with TemporaryDirectory() as temp_dir:
        location = StorageLocationRequest(legacy_root=Path(temp_dir))
        runtime = ApplicationRuntime.create_default()
        app = SecInstitutionalUniverseApplication(
            runtime,
            transport_factory=lambda: transport,
        )

        # 1. Refresh creates new snapshot and blob
        result = app.refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=sec_identity,
            location=location,
        )
        assert result.created is True
        assert result.candidate_manager_count == 3
        assert result.selected_manager_count == 3
        assert result.matched_asset_count == 1
        assert result.eligible_asset_count >= 1
        assert result.unselected_manager_count == 0
        assert result.coverage_complete is True

        # 2. Rerun is idempotent
        rerun_result = app.refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=sec_identity,
            location=location,
        )
        assert rerun_result.created is False
        assert rerun_result.revision_id == result.revision_id
        assert rerun_result.snapshot_id == result.snapshot_id
        assert rerun_result.coverage_complete is True

        # 3. Read-only query
        query_result = app.query(
            SecInstitutionalUniverseQueryRequest(
                known_at=result.available_at,
                asset_id="equity:us:aapl",
            ),
            location=location,
        )
        assert query_result.snapshot.snapshot_id == result.snapshot_id
        assert len(query_result.filtered_candidates) == 3

        # 4. Filter by CIK
        filtered_query = app.query(
            SecInstitutionalUniverseQueryRequest(
                known_at=result.available_at,
                cik="0001067983",
            ),
            location=location,
        )
        assert len(filtered_query.filtered_candidates) == 1
        assert filtered_query.filtered_candidates[0].manager_cik == "0001067983"

        # 5. Querying before available_at raises error (strict PIT)
        past_time = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
        with pytest.raises(
            SecInstitutionalUniverseApplicationError,
            match="No Form 13F manager universe snapshot available",
        ):
            app.query(
                SecInstitutionalUniverseQueryRequest(known_at=past_time),
                location=location,
            )
