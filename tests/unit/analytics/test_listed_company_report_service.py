"""Tests for generic listed-company report assembly."""

from datetime import UTC, datetime

from investment_analyst.analytics.listed_company_report_models import ListedCompanyReportRequest
from investment_analyst.analytics.listed_company_report_service import ListedCompanyReportService
from investment_analyst.core.models import DataFrequency
from investment_analyst.storage import LocalStorage, StoragePaths


def test_generic_report_is_read_only_and_keeps_missing_evidence_distinct(tmp_path) -> None:
    request = ListedCompanyReportRequest(
        asset_id="equity:us:amd",
        known_at=datetime(2026, 8, 1, tzinfo=UTC),
        fundamental_frequency=DataFrequency.QUARTERLY,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        before = storage.store.connection.execute(
            "SELECT COUNT(*) FROM diagnostic_results"
        ).fetchone()[0]
        report = ListedCompanyReportService(storage).query(
            request,
            symbol="AMD",
            name="Advanced Micro Devices",
            source_ids=("alpaca:amd", "sec:companyfacts", "sec:submissions"),
        )
        after = storage.store.connection.execute(
            "SELECT COUNT(*) FROM diagnostic_results"
        ).fetchone()[0]

    assert before == after == 0
    assert report.to_json_dict()["schema_version"] == "listed-company-diagnostic-report-v1"
    assert report.to_json_dict()["asset"]["asset_id"] == "equity:us:amd"
    assert report.to_json_dict()["fundamental"]["status"] == "not_found"
    assert "combined_score" not in report.to_json_dict()
