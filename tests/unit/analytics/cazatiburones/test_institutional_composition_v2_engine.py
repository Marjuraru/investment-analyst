"""Unit tests for institutional composition v2 engine."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_composition_v2_engine import (
    resolve_v2,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_v2_models import (
    InstitutionalCompositionV2Candidate,
)

_MANAGER = "0001067983"
_PERIOD = date(2024, 12, 31)
_AVAILABLE = datetime(2025, 2, 14, tzinfo=UTC)


def _candidate(**updates: object) -> InstitutionalCompositionV2Candidate:
    values: dict[str, object] = {
        "artifact_id": uuid4(),
        "accession": "0000950123-25-000001",
        "manager_cik": _MANAGER,
        "report_period": _PERIOD,
        "available_at": _AVAILABLE,
        "is_amendment": False,
        "declared_entry_total": 1,
        "declared_value_total": Decimal("0.10"),
        "observed_entry_total": 1,
        "observed_value_total": Decimal("0.10"),
    }
    values.update(updates)
    return InstitutionalCompositionV2Candidate(**values)


def _resolve(*candidates: InstitutionalCompositionV2Candidate, known_at: datetime = _AVAILABLE):
    return resolve_v2(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=known_at,
        candidates=tuple(candidates),
    )


def test_v2_point_in_time_known_at_ignores_future_amendments() -> None:
    original = _candidate()
    future = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
    )

    result = _resolve(original, future)

    assert result.status == "original_complete"
    assert result.effective_artifact_id == original.artifact_id
    assert result.source_literal is None
    assert result.operation is None


def test_v2_restatement_maps_to_replacement() -> None:
    original = _candidate()
    amendment = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
    )

    result = _resolve(original, amendment, known_at=_AVAILABLE + timedelta(days=1))

    assert result.status == "amended"
    assert result.reason == "declared_amendment_restatement"
    assert result.source_literal == "RESTATEMENT"
    assert result.operation == "replacement"
    assert result.effective_artifact_id == amendment.artifact_id


def test_v2_new_holdings_maps_to_supplement() -> None:
    original = _candidate()
    amendment = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS",
    )

    result = _resolve(original, amendment, known_at=_AVAILABLE + timedelta(days=1))

    assert result.status == "amended"
    assert result.reason == "declared_amendment_new_holdings"
    assert result.source_literal == "NEW HOLDINGS"
    assert result.operation == "supplement"
    assert result.effective_artifact_id == amendment.artifact_id


def test_v2_rejects_legacy_new_holdings_entries_as_unknown() -> None:
    original = _candidate()
    amendment = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS ENTRIES",
    )

    result = _resolve(original, amendment, known_at=_AVAILABLE + timedelta(days=1))

    assert result.status == "ambiguous"
    assert result.reason == "unknown_amendment_type"
    assert result.effective_artifact_id is None


def test_v2_multiple_originals_fail_closed() -> None:
    first_original = _candidate(accession="0000950123-25-000001")
    second_original = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(hours=1),
    )

    result = _resolve(first_original, second_original, known_at=_AVAILABLE + timedelta(hours=1))

    assert result.status == "ambiguous"
    assert result.reason == "contradictory_amendment_chain"


def test_v2_amendment_gap_fails_closed() -> None:
    original = _candidate()
    amendment_two = _candidate(
        accession="0000950123-25-000003",
        available_at=_AVAILABLE + timedelta(days=2),
        is_amendment=True,
        amendment_number="2",
        amendment_type="RESTATEMENT",
    )

    result = _resolve(original, amendment_two, known_at=_AVAILABLE + timedelta(days=2))

    assert result.status == "insufficient"
    assert result.reason == "amendment_chain_incomplete"
