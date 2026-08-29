from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_index import (
    institutional_holdings_filings,
)


def _record(*, report_date: object = "", accepted_at: object = "2025-02-14T18:00:00Z"):
    captured = datetime(2025, 2, 15, tzinfo=UTC)
    return RawRecord(
        record_id=uuid4(),
        asset_id=None,
        source=SourceReference(
            source_id="sec-edgar:manager:0001067983:submissions", retrieved_at=captured
        ),
        event_time=captured,
        available_at=captured,
        received_at=captured,
        payload={
            "document": {
                "cik": "1067983",
                "name": "Manager LLC",
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000950123-25-000001", "0000950123-25-000002"],
                        "filingDate": ["2025-02-14", "2025-02-14"],
                        "reportDate": [report_date, "2024-12-31"],
                        "acceptanceDateTime": [accepted_at, "2025-02-14T19:00:00Z"],
                        "form": ["13F-HR", "13F-NT"],
                        "primaryDocument": ["primary_doc.xml", "notice.xml"],
                    }
                },
            }
        },
        schema_version="sec-manager-submissions-snapshot-v1",
    )


def test_index_selects_only_holdings_and_preserves_missing_period() -> None:
    filings = institutional_holdings_filings(_record(), "1067983")

    assert len(filings) == 1
    assert filings[0].form == "13F-HR"
    assert filings[0].report_date is None


def test_index_rejects_invalid_present_period_and_missing_acceptance() -> None:
    with pytest.raises(ValueError, match="invalid institutional"):
        institutional_holdings_filings(_record(report_date="not-a-date"), "1067983")
    with pytest.raises(ValueError, match="invalid institutional"):
        institutional_holdings_filings(_record(accepted_at=""), "1067983")
    with pytest.raises(ValueError, match="invalid institutional"):
        institutional_holdings_filings(_record(accepted_at="2025-02-14T18:00:00"), "1067983")
