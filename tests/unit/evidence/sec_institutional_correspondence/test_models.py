"""Unit tests for the strict, closed shape of a row-scoped correspondence claim."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_POLICY_VERSION,
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
    ROW_CORRESPONDENCE_SOURCE_ID,
    SecInstitutionalRowCorrespondence,
)

_REPORT_PERIOD = date(2026, 3, 31)
_AVAILABLE_AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_RECORDED_AT = datetime(2026, 7, 3, 9, 30, tzinfo=UTC)


def _claim(**overrides: object) -> SecInstitutionalRowCorrespondence:
    values: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "cusip": "037833100",
        "title_of_class": "COM",
        "report_period": _REPORT_PERIOD,
        "manager_cik": "0002012383",
        "report_id": uuid4(),
        "artifact_id": uuid4(),
        "row_id": uuid4(),
        "universe_snapshot_id": uuid4(),
        "dataset_revision_id": uuid4(),
        "candidate_id": uuid4(),
        "available_at": _AVAILABLE_AT,
        "recorded_at": _RECORDED_AT,
    }
    values.update(overrides)
    return SecInstitutionalRowCorrespondence.claim(**values)  # type: ignore[arg-type]


def test_claim_declares_a_closed_window_and_a_versioned_identity() -> None:
    claim = _claim()
    assert claim.effective_from == _REPORT_PERIOD
    assert claim.effective_to == date(2026, 4, 1)
    assert claim.effective_to == date.fromordinal(_REPORT_PERIOD.toordinal() + 1)
    assert claim.event_time == datetime(2026, 3, 31, tzinfo=UTC)
    assert claim.policy_version == ROW_CORRESPONDENCE_POLICY_VERSION
    assert claim.schema_version == ROW_CORRESPONDENCE_SCHEMA_VERSION
    assert ROW_CORRESPONDENCE_SOURCE_ID == "sec-edgar:institutional-row-correspondence"
    assert claim.covers(_REPORT_PERIOD) is True
    assert claim.covers(date(2025, 12, 31)) is False
    assert claim.covers(None) is False
    assert claim.recorded_at >= claim.available_at


def test_claim_identity_is_deterministic_and_reusable() -> None:
    first = _claim()
    second = _claim(
        asset_id=first.asset_id,
        cusip=first.cusip,
        title_of_class=first.title_of_class,
        report_period=first.report_period,
        manager_cik=first.manager_cik,
        report_id=first.report_id,
        artifact_id=first.artifact_id,
        row_id=first.row_id,
        universe_snapshot_id=first.universe_snapshot_id,
        dataset_revision_id=first.dataset_revision_id,
        candidate_id=first.candidate_id,
        available_at=first.available_at,
        recorded_at=_RECORDED_AT,
    )
    assert second.correspondence_id == first.correspondence_id
    assert second.raw_record_id == first.raw_record_id
    assert first.raw_record_id == SecInstitutionalRowCorrespondence.expected_raw_record_id(
        first.correspondence_id
    )

    later_record = _claim(
        asset_id=first.asset_id,
        cusip=first.cusip,
        title_of_class=first.title_of_class,
        report_period=first.report_period,
        manager_cik=first.manager_cik,
        report_id=first.report_id,
        artifact_id=first.artifact_id,
        row_id=first.row_id,
        universe_snapshot_id=first.universe_snapshot_id,
        dataset_revision_id=first.dataset_revision_id,
        candidate_id=first.candidate_id,
        available_at=first.available_at,
        recorded_at=_RECORDED_AT.replace(day=4),
    )
    assert later_record.correspondence_id == first.correspondence_id

    assert _claim().correspondence_id != first.correspondence_id
    assert _claim(row_id=first.row_id).correspondence_id != first.correspondence_id
    assert (
        _claim(
            candidate_id=first.candidate_id,
            universe_snapshot_id=uuid4(),
            report_id=first.report_id,
            artifact_id=first.artifact_id,
            row_id=first.row_id,
        ).correspondence_id
        != first.correspondence_id
    )
    assert (
        _claim(
            candidate_id=first.candidate_id,
            report_id=first.report_id,
            artifact_id=first.artifact_id,
            row_id=first.row_id,
            available_at=_AVAILABLE_AT.replace(hour=13),
        ).correspondence_id
        != first.correspondence_id
    )
    assert (
        _claim(
            candidate_id=first.candidate_id,
            report_id=first.report_id,
            artifact_id=first.artifact_id,
            row_id=first.row_id,
            title_of_class="COM CL A",
        ).correspondence_id
        != first.correspondence_id
    )


def test_claim_normalizes_the_manager_cik() -> None:
    claim = _claim(manager_cik="2012383")
    assert claim.manager_cik == "0002012383"


def test_claim_rejects_an_open_or_extended_validity_window() -> None:
    claim = _claim()
    for name, value in (
        ("effective_from", date(2026, 3, 30)),
        ("effective_to", date(2026, 4, 2)),
        ("effective_to", date(2026, 3, 31)),
    ):
        with pytest.raises(ValidationError):
            SecInstitutionalRowCorrespondence.model_validate({**claim.model_dump(), name: value})


def test_claim_rejects_inconsistent_availability_and_identity() -> None:
    claim = _claim()
    for name, value in (
        ("recorded_at", datetime(2026, 6, 1, tzinfo=UTC)),
        ("correspondence_id", uuid4()),
        ("raw_record_id", uuid4()),
        ("cusip", "037833101"),
        ("cusip", "03783310"),
    ):
        with pytest.raises(ValidationError):
            SecInstitutionalRowCorrespondence.model_validate({**claim.model_dump(), name: value})


def test_claim_is_strict_and_forbids_extra_fields() -> None:
    claim = _claim()
    with pytest.raises(ValidationError):
        SecInstitutionalRowCorrespondence.model_validate({**claim.model_dump(), "ticker": "AAPL"})
    with pytest.raises(ValidationError):
        SecInstitutionalRowCorrespondence.model_validate(
            {**claim.model_dump(), "issuer_name": "APPLE INC"}
        )
    with pytest.raises(ValidationError):
        SecInstitutionalRowCorrespondence.model_validate(
            {**claim.model_dump(), "figi": "BBG000B9XRY4"}
        )
    with pytest.raises(ValidationError):
        SecInstitutionalRowCorrespondence.model_validate(
            {**claim.model_dump(), "cusip": UUID(int=0)}
        )
    with pytest.raises(ValueError):
        _claim(recorded_at=datetime(2026, 7, 3))
    with pytest.raises(ValueError):
        _claim(available_at=datetime(2026, 7, 1))
    with pytest.raises(ValidationError):
        SecInstitutionalRowCorrespondence.model_validate(
            {**claim.model_dump(), "recorded_at": datetime(2026, 7, 3)}
        )
    incomplete = claim.model_dump()
    incomplete.pop("asset_id")
    with pytest.raises(ValidationError):
        SecInstitutionalRowCorrespondence.model_validate(incomplete)
