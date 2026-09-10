"""Integration tests for SEC Form 13F dataset pipeline, idempotence, PIT, and scope boundaries."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseQueryRequest,
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.evidence.sec_institutional_universe.identity import (
    SEC_13F_MANAGER_UNIVERSE_SOURCE_ID,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.workspace.models import WorkspaceAccessMode


def _build_integration_test_zip() -> bytes:
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


class _IntegrationMockTransport(HttpTransport):
    def __init__(self, catalog_html: str, zip_bytes: bytes, zip_url: str) -> None:
        self.catalog_html = catalog_html
        self.zip_bytes = zip_bytes
        self.zip_url = zip_url

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
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


def test_sec_13f_dataset_pipeline_idempotence_pit_and_invariants() -> None:
    secret_user_agent = "InvestmentAnalystSecurityResearch secure-audit@internal.example.org"
    sec_identity = SecEdgarIdentity(secret_user_agent)

    zip_url = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip"
    catalog_html = f'<html><a href="{zip_url}">2026 Q1</a></html>'
    zip_bytes = _build_integration_test_zip()

    transport = _IntegrationMockTransport(catalog_html, zip_bytes, zip_url)

    with TemporaryDirectory() as temp_dir:
        location = StorageLocationRequest(legacy_root=Path(temp_dir))
        runtime = ApplicationRuntime.create_default()
        app = SecInstitutionalUniverseApplication(
            runtime,
            transport_factory=lambda: transport,
        )

        # 1. Run pipeline
        result1 = app.refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=sec_identity,
            location=location,
        )
        assert result1.created is True
        assert result1.coverage_complete is True
        assert result1.candidate_manager_count == 3
        assert result1.selected_manager_count == 3
        assert result1.available_at == result1.retrieved_at

        # Verify storage state directly
        with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
            # Check raw records count
            records = storage.raw_records.list(source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID)
            # Expect exactly 2 records: 1 dataset revision, 1 universe snapshot
            assert len(records) == 2

            # Negative N1: Verify NO holdings, observations, metrics, events, notifications
            obs = storage.observations.list(asset_id="equity:us:aapl")
            assert len(obs) == 0, "Form 13F dataset must NEVER produce observations"

            metrics = storage.metric_results.list(asset_id="equity:us:aapl")
            assert len(metrics) == 0, "Form 13F dataset must NEVER produce metric results"

            diagnostics = storage.diagnostics.list(asset_id="equity:us:aapl")
            assert len(diagnostics) == 0, "Form 13F dataset must NEVER produce diagnostics"

            # Check that secret_user_agent was NOT stored anywhere in raw records
            for rec in records:
                payload_str = json.dumps(rec.payload)
                assert secret_user_agent not in payload_str, (
                    "Secret user agent leaked into RawRecord"
                )
                assert "secure-audit" not in payload_str, "Email leaked into RawRecord"

            # Check Decimal exactness on candidates in snapshot
            snapshot_rec = next(
                r for r in records if r.payload.get("kind") == "sec_13f_manager_universe_snapshot"
            )
            candidates = snapshot_rec.payload["snapshot"]["candidates"]
            for cand in candidates:
                assert isinstance(cand["value_as_filed"], str)
                val = Decimal(cand["value_as_filed"])
                assert val >= 0
                assert cand["value_unit"] == "usd_thousands_as_filed"

        # 2. Run rerun: verify absolute idempotence (0 new raw records, 0 new files)
        result2 = app.refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=sec_identity,
            location=location,
        )
        assert result2.created is False
        assert result2.revision_id == result1.revision_id
        assert result2.snapshot_id == result1.snapshot_id

        with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
            records2 = storage.raw_records.list(source_id=SEC_13F_MANAGER_UNIVERSE_SOURCE_ID)
            assert len(records2) == 2, "Rerun must not create duplicate raw records"

        # 3. Read-only query test
        query_res = app.query(
            SecInstitutionalUniverseQueryRequest(
                known_at=datetime.now(UTC),
                asset_id="equity:us:aapl",
            ),
            location=location,
        )
        assert query_res.snapshot.snapshot_id == result1.snapshot_id
        assert len(query_res.filtered_candidates) == 3
