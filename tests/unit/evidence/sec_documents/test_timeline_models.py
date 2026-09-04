"""Unit tests for strict, immutable SEC document timeline contracts."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.evidence.sec_documents.timeline_models import (
    SecDocumentTimelineEntry,
    SecDocumentTimelineQuery,
    SecDocumentTimelineResult,
)

_KNOWN_AT = datetime(2025, 2, 15, 18, tzinfo=UTC)
_CHECKSUM = "a" * 64


def _entry(
    family: str,
    asset_id: str | None = None,
    cik: str = "0001067983",
    form: str = "10-K",
    accession: str = "0000950123-25-000001",
) -> SecDocumentTimelineEntry:
    return SecDocumentTimelineEntry(
        family=family,  # type: ignore[arg-type]
        revision_id=uuid4(),
        asset_id=asset_id,
        filer_cik=cik,
        form=form,
        accession=accession,
        document_name="primary.htm",
        filing_date=date(2025, 2, 14),
        report_date=date(2024, 12, 31),
        accepted_at=_KNOWN_AT,
        available_at=_KNOWN_AT,
        content_sha256=_CHECKSUM,
        content_size_bytes=1024,
        source_url="https://www.sec.gov/Archives/primary.htm",
        is_amendment=form.endswith("/A"),
    )


def test_query_requires_asset_or_filer_scope() -> None:
    with pytest.raises(ValidationError, match="at least one asset_id or filer_cik"):
        SecDocumentTimelineQuery(known_at=_KNOWN_AT, asset_ids=(), filer_ciks=())

    with pytest.raises(ValidationError, match="at least one asset_id or filer_cik"):
        SecDocumentTimelineQuery(known_at=_KNOWN_AT)

    q1 = SecDocumentTimelineQuery(known_at=_KNOWN_AT, asset_ids=("equity:us:aapl",))
    assert q1.asset_ids == ("equity:us:aapl",)
    assert q1.filer_ciks == ()

    q2 = SecDocumentTimelineQuery(known_at=_KNOWN_AT, filer_ciks=("1067983",))
    assert q2.filer_ciks == ("0001067983",)

    q3 = SecDocumentTimelineQuery(
        known_at=_KNOWN_AT,
        asset_ids=["equity:us:aapl"],
        filer_ciks=["0001067983"],
    )
    assert q3.asset_ids == ("equity:us:aapl",)
    assert q3.filer_ciks == ("0001067983",)


def test_query_date_and_form_validation() -> None:
    with pytest.raises(ValidationError, match="available_from must be on or before available_to"):
        SecDocumentTimelineQuery(
            known_at=_KNOWN_AT,
            asset_ids=("equity:us:aapl",),
            available_from=date(2025, 2, 20),
            available_to=date(2025, 2, 10),
        )

    with pytest.raises(ValidationError, match="outside the SEC corpus v1 family"):
        SecDocumentTimelineQuery(
            known_at=_KNOWN_AT,
            asset_ids=("equity:us:aapl",),
            forms=("UNKNOWN_FORM",),
        )

    with pytest.raises(ValidationError, match="accession must use the SEC accession format"):
        SecDocumentTimelineQuery(
            known_at=_KNOWN_AT,
            asset_ids=("equity:us:aapl",),
            accession="invalid-accession",
        )


def test_asset_id_present_only_in_asset_document_family() -> None:
    asset_entry = _entry("asset_document", asset_id="equity:us:aapl")
    assert asset_entry.family == "asset_document"
    assert asset_entry.asset_id == "equity:us:aapl"

    with pytest.raises(ValidationError, match="asset_id is required for asset_document family"):
        _entry("asset_document", asset_id=None)

    filer_entry = _entry("filer_document", asset_id=None, form="13F-HR")
    assert filer_entry.family == "filer_document"
    assert filer_entry.asset_id is None

    with pytest.raises(ValidationError, match="asset_id must not be present for filer_document"):
        _entry("filer_document", asset_id="equity:us:aapl", form="13F-HR")


def test_missing_state_is_explicit_and_never_zero() -> None:
    missing_result = SecDocumentTimelineResult(
        state="missing",
        known_at=_KNOWN_AT,
        entries=(),
        matched_count=0,
        returned_count=0,
        legacy_records_excluded=2,
        truncated=False,
    )
    assert missing_result.state == "missing"
    assert len(missing_result.entries) == 0
    assert missing_result.legacy_records_excluded == 2

    entry = _entry("asset_document", asset_id="equity:us:aapl")
    with pytest.raises(ValidationError, match="missing result cannot contain matched entries"):
        SecDocumentTimelineResult(
            state="missing",
            known_at=_KNOWN_AT,
            entries=(entry,),
            matched_count=1,
            returned_count=1,
        )

    with pytest.raises(ValidationError, match="missing result cannot contain matched entries"):
        SecDocumentTimelineResult(
            state="missing",
            known_at=_KNOWN_AT,
            entries=(),
            matched_count=1,
            returned_count=0,
            truncated=True,
        )

    with pytest.raises(ValidationError, match="found result requires at least one matched entry"):
        SecDocumentTimelineResult(
            state="found",
            known_at=_KNOWN_AT,
            entries=(),
            matched_count=0,
            returned_count=0,
        )


def test_matched_returned_and_truncated_are_coherent() -> None:
    entry = _entry("asset_document", asset_id="equity:us:aapl")

    valid_found = SecDocumentTimelineResult(
        state="found",
        known_at=_KNOWN_AT,
        entries=(entry,),
        matched_count=1,
        returned_count=1,
        truncated=False,
    )
    assert valid_found.state == "found"
    assert valid_found.truncated is False

    valid_truncated = SecDocumentTimelineResult(
        state="found",
        known_at=_KNOWN_AT,
        entries=(entry,),
        matched_count=5,
        returned_count=1,
        truncated=True,
    )
    assert valid_truncated.truncated is True

    with pytest.raises(ValidationError, match="returned_count must match the number of entries"):
        SecDocumentTimelineResult(
            state="found",
            known_at=_KNOWN_AT,
            entries=(entry,),
            matched_count=1,
            returned_count=2,
        )

    with pytest.raises(ValidationError, match="truncated must be true if and only if"):
        SecDocumentTimelineResult(
            state="found",
            known_at=_KNOWN_AT,
            entries=(entry,),
            matched_count=1,
            returned_count=1,
            truncated=True,
        )

    with pytest.raises(ValidationError, match="truncated must be true if and only if"):
        SecDocumentTimelineResult(
            state="found",
            known_at=_KNOWN_AT,
            entries=(entry,),
            matched_count=5,
            returned_count=1,
            truncated=False,
        )


def test_families_never_merge_identity_or_namespace() -> None:
    asset_entry = _entry("asset_document", asset_id="equity:us:aapl")
    filer_entry = _entry("filer_document", asset_id=None, form="13F-HR")

    assert asset_entry.family != filer_entry.family
    assert asset_entry.asset_id is not None
    assert filer_entry.asset_id is None


def test_no_asset_inferred_from_filer_cik_or_inverse() -> None:
    filer_entry = _entry("filer_document", asset_id=None, cik="0001067983", form="13F-HR")
    assert filer_entry.asset_id is None

    asset_entry = _entry("asset_document", asset_id="equity:us:aapl", cik="0000320193", form="10-K")
    assert asset_entry.asset_id == "equity:us:aapl"
    assert asset_entry.filer_cik == "0000320193"


def test_query_without_scope_fails_closed() -> None:
    with pytest.raises(ValidationError):
        SecDocumentTimelineQuery.model_validate({"known_at": _KNOWN_AT.isoformat()})

    with pytest.raises(ValidationError):
        SecDocumentTimelineQuery.model_validate(
            {"known_at": _KNOWN_AT.isoformat(), "asset_ids": [], "filer_ciks": []}
        )


def test_no_score_ranking_relevance_or_recommendation_emitted() -> None:
    forbidden_terms = {
        "score",
        "rank",
        "ranking",
        "relevance",
        "recommendation",
        "signal",
        "verdict",
        "percentile",
        "confidence",
        "quality",
    }
    entry_fields = set(SecDocumentTimelineEntry.model_fields.keys())
    result_fields = set(SecDocumentTimelineResult.model_fields.keys())
    query_fields = set(SecDocumentTimelineQuery.model_fields.keys())

    assert entry_fields.isdisjoint(forbidden_terms)
    assert result_fields.isdisjoint(forbidden_terms)
    assert query_fields.isdisjoint(forbidden_terms)


def test_models_are_frozen_and_strict() -> None:
    entry = _entry("asset_document", asset_id="equity:us:aapl")
    with pytest.raises(ValidationError):
        entry.document_name = "modified.htm"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        SecDocumentTimelineQuery(
            known_at=_KNOWN_AT,
            asset_ids=("equity:us:aapl",),
            unexpected_field="disallowed",  # type: ignore[call-arg]
        )
