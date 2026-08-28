from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investment_analyst.core.models import AssetClass, RawRecord, SourceReference
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_index import (
    beneficial_ownership_filings,
)


def _configuration() -> SecAssetConfiguration:
    return SecAssetConfiguration(
        asset_id="equity:us:aapl",
        cik="0000320193",
        ticker="AAPL",
        submissions_source_id="sec-edgar:aapl:submissions",
        companyfacts_source_id="sec-edgar:aapl:companyfacts",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def _record() -> RawRecord:
    captured = datetime(2025, 2, 1, tzinfo=UTC)
    return RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="sec-edgar:aapl:submissions", retrieved_at=captured),
        event_time=captured,
        available_at=captured,
        received_at=captured,
        payload={
            "document": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001193125-24-036431", "0000320193-25-000002"],
                        "filingDate": ["2025-01-31", "2025-01-31"],
                        "acceptanceDateTime": [
                            "2025-01-31T18:00:00.000Z",
                            "2025-01-31T19:00:00.000Z",
                        ],
                        "form": ["SC 13G", "4"],
                        "primaryDocument": ["filing.htm", "form4.xml"],
                    }
                }
            }
        },
        schema_version="sec-edgar-submissions-snapshot-v1",
    )


def _with_report_date(record: RawRecord, *, report_date: str | None, present: bool) -> RawRecord:
    recent = record.payload["document"]["filings"]["recent"]
    if present:
        recent["reportDate"] = [report_date, "2025-01-30"]
    else:
        recent.pop("reportDate", None)
    return record


def test_index_selects_only_13d_13g_forms() -> None:
    filings = beneficial_ownership_filings(_record(), _configuration())

    assert [item.form for item in filings] == ["SC 13G"]
    assert filings[0].accepted_at == datetime(2025, 1, 31, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("report_date", "present"),
    (("", True), (None, False)),
)
def test_index_preserves_empty_or_absent_report_date_as_missing(
    report_date: str | None, present: bool
) -> None:
    record = _with_report_date(_record(), report_date=report_date, present=present)

    (filing,) = beneficial_ownership_filings(record, _configuration())

    assert filing.accession == "0001193125-24-036431"
    assert filing.report_date is None


def test_index_rejects_non_iso_report_date_without_degrading_to_missing() -> None:
    record = _with_report_date(_record(), report_date="not-a-date", present=True)
    with pytest.raises(ValueError, match="invalid beneficial ownership filing"):
        beneficial_ownership_filings(record, _configuration())
