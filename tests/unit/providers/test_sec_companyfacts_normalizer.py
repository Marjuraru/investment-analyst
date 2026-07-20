"""Tests for exact Apple SEC Company Facts selection and observation conversion."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.core.models import DataFrequency, RawRecord, SourceReference
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    ConflictingSecFactError,
    MalformedSecCompanyFactsError,
    SecCompanyFactsNormalizer,
    sec_fact_to_observation,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SCHEMA_VERSION,
    COMPANYFACTS_SOURCE_ID,
    SEC_RESEARCH_FACT_DEFINITIONS,
    SUBMISSIONS_SCHEMA_VERSION,
    SUBMISSIONS_SOURCE_ID,
    SecFactPeriodType,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    allowed_sec_fundamental_fields,
)

RETRIEVED_AT = datetime(2026, 7, 13, 18, tzinfo=UTC)
NORMALIZED_AT = RETRIEVED_AT + timedelta(hours=1)
ANNUAL_ACCN = "0000320193-26-000001"
QUARTERLY_ACCN = "0000320193-26-000002"


def _submissions_document() -> dict[str, object]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "filings": {
            "recent": {
                "accessionNumber": [ANNUAL_ACCN, QUARTERLY_ACCN],
                "filingDate": ["2026-01-30", "2026-04-30"],
                "reportDate": ["2025-12-31", "2026-03-31"],
                "acceptanceDateTime": [
                    "2026-01-30T21:00:00Z",
                    "2026-04-30T21:00:00Z",
                ],
                "form": ["10-K", "10-Q"],
                "primaryDocument": [
                    "aapl-20251231x10k.htm",
                    "aapl-20260331x10q.htm",
                ],
            },
            "files": [],
        },
    }


def _duration_fact(
    *,
    annual: bool,
    value: str,
    tag_frame: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "start": "2025-01-01" if annual else "2026-01-01",
        "end": "2025-12-31" if annual else "2026-03-31",
        "val": value,
        "accn": ANNUAL_ACCN if annual else QUARTERLY_ACCN,
        "fy": "2025" if annual else "2026",
        "fp": "FY" if annual else "Q1",
        "form": "10-K" if annual else "10-Q",
        "filed": "2026-01-30" if annual else "2026-04-30",
    }
    if tag_frame is not None:
        result["frame"] = tag_frame
    return result


def _instant_fact(*, annual: bool, value: str) -> dict[str, object]:
    return {
        "end": "2025-12-31" if annual else "2026-03-31",
        "val": value,
        "accn": ANNUAL_ACCN if annual else QUARTERLY_ACCN,
        "fy": "2025" if annual else "2026",
        "fp": "FY" if annual else "Q1",
        "form": "10-K" if annual else "10-Q",
        "filed": "2026-01-30" if annual else "2026-04-30",
    }


def _company_document() -> dict[str, object]:
    concepts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            _duration_fact(annual=True, value="1000.25"),
            _duration_fact(annual=False, value="260.50"),
        ],
        "NetIncomeLoss": [
            _duration_fact(annual=True, value="200.10"),
            _duration_fact(annual=False, value="52.25"),
        ],
        "Assets": [
            _instant_fact(annual=True, value="5000"),
            _instant_fact(annual=False, value="5100"),
        ],
        "Liabilities": [
            _instant_fact(annual=True, value="3000"),
            _instant_fact(annual=False, value="3050"),
        ],
        "StockholdersEquity": [
            _instant_fact(annual=True, value="2000"),
            _instant_fact(annual=False, value="2050"),
        ],
    }
    return {
        "cik": "0000320193",
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {tag: {"units": {"USD": facts}} for tag, facts in concepts.items()},
            "dei": {"Synthetic": {"units": {"USD": []}}},
        },
    }


def _company_document_with_research_facts() -> dict[str, object]:
    document = _company_document()
    us_gaap = document["facts"]["us-gaap"]
    for position, definition in enumerate(SEC_RESEARCH_FACT_DEFINITIONS, start=1):
        if definition.period_type is SecFactPeriodType.DURATION:
            facts = [
                _duration_fact(annual=True, value=str(1000 + position)),
                _duration_fact(annual=False, value=str(200 + position)),
            ]
        else:
            facts = [
                _instant_fact(annual=True, value=str(5000 + position)),
                _instant_fact(annual=False, value=str(5100 + position)),
            ]
        us_gaap[definition.tag] = {"units": {"USD": facts}}
    return document


def _record(
    *,
    source_id: str,
    schema_version: str,
    document_type: str,
    document: dict[str, object],
    record_id=None,
    retrieved_at: datetime = RETRIEVED_AT,
    checksum: str = "a" * 64,
) -> RawRecord:
    return RawRecord(
        record_id=record_id or uuid4(),
        asset_id=ASSET_ID,
        source=SourceReference(
            source_id=source_id,
            retrieved_at=retrieved_at,
            raw_uri=f"https://data.sec.gov/{document_type}.json",
            checksum_sha256=checksum,
        ),
        event_time=retrieved_at,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={
            "document_type": document_type,
            "cik": "0000320193",
            "entity_name": "Apple Inc.",
            "document": document,
        },
        schema_version=schema_version,
    )


def _submissions(**overrides: object) -> RawRecord:
    values = {
        "source_id": SUBMISSIONS_SOURCE_ID,
        "schema_version": SUBMISSIONS_SCHEMA_VERSION,
        "document_type": "submissions",
        "document": _submissions_document(),
    }
    values.update(overrides)
    return _record(**values)


def _company(**overrides: object) -> RawRecord:
    values = {
        "source_id": COMPANYFACTS_SOURCE_ID,
        "schema_version": COMPANYFACTS_SCHEMA_VERSION,
        "document_type": "company_facts",
        "document": _company_document(),
    }
    values.update(overrides)
    return _record(**values)


def test_extracts_all_five_annual_and_quarterly_fields() -> None:
    result = SecCompanyFactsNormalizer().extract(
        _company(),
        _submissions(),
        normalized_at=NORMALIZED_AT,
    )

    assert result.facts_examined == 10
    assert result.facts_selected == 10
    assert result.annual_count == 5
    assert result.quarterly_count == 5
    assert {field_name for field_name, count in result.field_counts.items() if count} == {
        "fundamental.revenue",
        "fundamental.net_income",
        "fundamental.assets",
        "fundamental.liabilities",
        "fundamental.stockholders_equity",
    }
    assert all(result.field_counts[item.field_name] == 0 for item in SEC_RESEARCH_FACT_DEFINITIONS)
    assert {fact.value for fact in result.facts} >= {Decimal("1000.25"), Decimal("260.50")}


def test_extracts_additional_research_catalog_without_expanding_core_query() -> None:
    result = SecCompanyFactsNormalizer().extract(
        _company(document=_company_document_with_research_facts()),
        _submissions(),
        normalized_at=NORMALIZED_AT,
    )

    expected_total_fields = 5 + len(SEC_RESEARCH_FACT_DEFINITIONS)
    assert result.facts_examined == expected_total_fields * 2
    assert result.facts_selected == expected_total_fields * 2
    assert result.annual_count == expected_total_fields
    assert result.quarterly_count == expected_total_fields
    assert all(result.field_counts[item.field_name] == 2 for item in SEC_RESEARCH_FACT_DEFINITIONS)
    assert set(allowed_sec_fundamental_fields()) == {
        "fundamental.revenue",
        "fundamental.net_income",
        "fundamental.assets",
        "fundamental.liabilities",
        "fundamental.stockholders_equity",
    }


def test_excludes_ytd_comparatives_q4_and_non_usd() -> None:
    document = _company_document()
    revenue = document["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    revenue["units"]["USD"].extend(
        [
            {
                **_duration_fact(annual=False, value="500"),
                "start": "2025-10-01",
                "end": "2025-12-31",
                "accn": ANNUAL_ACCN,
                "fy": "2025",
                "fp": "Q4",
                "form": "10-K",
                "filed": "2026-01-30",
            },
            {**_duration_fact(annual=False, value="520"), "start": "2025-10-01"},
            {**_duration_fact(annual=False, value="600"), "start": "2025-10-01"},
            {**_duration_fact(annual=False, value="700"), "start": "2025-07-01"},
        ]
    )
    revenue["units"]["EUR"] = [_duration_fact(annual=True, value="999")]

    result = SecCompanyFactsNormalizer().extract(
        _company(document=document),
        _submissions(),
        normalized_at=NORMALIZED_AT,
    )

    revenue_facts = [fact for fact in result.facts if fact.field_name == "fundamental.revenue"]
    assert len(revenue_facts) == 2
    assert result.skipped_counts["unsupported_fiscal_period"] >= 1
    assert result.skipped_counts["non_discrete_quarter_duration"] >= 3


def test_metadata_mismatches_and_missing_accession_are_skipped() -> None:
    document = _company_document()
    assets = document["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    assets.extend(
        [
            {**_instant_fact(annual=True, value="1"), "accn": "missing"},
            {**_instant_fact(annual=True, value="2"), "form": "10-Q"},
            {**_instant_fact(annual=True, value="3"), "filed": "2026-01-31"},
            {**_instant_fact(annual=True, value="4"), "end": "2025-12-30"},
        ]
    )

    result = SecCompanyFactsNormalizer().extract(
        _company(document=document),
        _submissions(),
        normalized_at=NORMALIZED_AT,
    )

    assert result.skipped_counts["missing_filing_metadata"] == 1
    assert result.skipped_counts["form_mismatch"] == 1
    assert result.skipped_counts["filed_date_mismatch"] == 1
    assert result.skipped_counts["report_date_mismatch"] == 1


def test_missing_tag_and_invalid_value_handling() -> None:
    missing = _company_document()
    del missing["facts"]["us-gaap"]["NetIncomeLoss"]
    result = SecCompanyFactsNormalizer().extract(
        _company(document=missing),
        _submissions(),
        normalized_at=NORMALIZED_AT,
    )
    assert result.field_counts["fundamental.net_income"] == 0
    assert result.skipped_counts["missing_concept:fundamental.net_income"] == 1

    invalid = _company_document()
    invalid["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["val"] = "not-a-number"
    with pytest.raises(MalformedSecCompanyFactsError, match="valid Decimal"):
        SecCompanyFactsNormalizer().extract(
            _company(document=invalid),
            _submissions(),
            normalized_at=NORMALIZED_AT,
        )


def test_duplicates_collapse_and_conflicting_values_fail() -> None:
    duplicate = _company_document()
    values = duplicate["facts"]["us-gaap"]["Assets"]["units"]["USD"]
    values.append(deepcopy(values[0]))
    result = SecCompanyFactsNormalizer().extract(
        _company(document=duplicate),
        _submissions(),
        normalized_at=NORMALIZED_AT,
    )
    assert result.facts_selected == 10
    assert result.skipped_counts["duplicate_identical"] == 1

    conflict = deepcopy(duplicate)
    conflict["facts"]["us-gaap"]["Assets"]["units"]["USD"][-1]["val"] = "5001"
    with pytest.raises(ConflictingSecFactError):
        SecCompanyFactsNormalizer().extract(
            _company(document=conflict),
            _submissions(),
            normalized_at=NORMALIZED_AT,
        )


def test_observation_uuid_is_snapshot_and_clock_independent_but_value_sensitive() -> None:
    submissions = _submissions()
    first_company = _company()
    second_company = _company(
        retrieved_at=RETRIEVED_AT + timedelta(days=1),
        checksum="b" * 64,
    )
    normalizer = SecCompanyFactsNormalizer()
    first_fact = normalizer.extract(
        first_company,
        submissions,
        normalized_at=NORMALIZED_AT,
    ).facts[0]
    second_fact = normalizer.extract(
        second_company,
        submissions,
        normalized_at=NORMALIZED_AT + timedelta(days=1),
    ).facts[0]
    first = sec_fact_to_observation(
        first_fact,
        first_company,
        submissions,
        normalized_at=NORMALIZED_AT,
    )
    second = sec_fact_to_observation(
        second_fact,
        second_company,
        submissions,
        normalized_at=NORMALIZED_AT + timedelta(days=1),
    )

    assert first.observation_id == second.observation_id
    assert first.raw_record_id != second.raw_record_id
    assert first.source.source_id == COMPANYFACTS_SOURCE_ID
    assert str(first_company.record_id) in first.source.record_key
    assert str(submissions.record_id) in first.source.record_key
    assert first.period_end.tzinfo is UTC
    assert first.frequency in {DataFrequency.ANNUAL, DataFrequency.QUARTERLY}

    annual_revenue = next(
        fact
        for fact in normalizer.extract(
            first_company,
            submissions,
            normalized_at=NORMALIZED_AT,
        ).facts
        if fact.field_name == "fundamental.revenue" and fact.frequency is DataFrequency.ANNUAL
    )
    stable_revenue = sec_fact_to_observation(
        annual_revenue,
        first_company,
        submissions,
        normalized_at=NORMALIZED_AT,
    )
    assert str(stable_revenue.observation_id) == "1854295b-134d-52f3-944e-c3e12cde9c77"

    revised_document = _company_document()
    revised_document["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["val"] = "5001"
    revised_company = _company(document=revised_document)
    revised_fact = next(
        fact
        for fact in normalizer.extract(
            revised_company,
            submissions,
            normalized_at=NORMALIZED_AT,
        ).facts
        if fact.field_name == "fundamental.assets" and fact.frequency is DataFrequency.ANNUAL
    )
    original_fact = next(
        fact
        for fact in normalizer.extract(
            first_company,
            submissions,
            normalized_at=NORMALIZED_AT,
        ).facts
        if fact.field_name == "fundamental.assets" and fact.frequency is DataFrequency.ANNUAL
    )
    original = sec_fact_to_observation(
        original_fact,
        first_company,
        submissions,
        normalized_at=NORMALIZED_AT,
    )
    revised = sec_fact_to_observation(
        revised_fact,
        revised_company,
        submissions,
        normalized_at=NORMALIZED_AT,
    )
    assert original.observation_id != revised.observation_id
