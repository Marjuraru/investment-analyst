from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionCandidate,
)

_MANAGER = "0001067983"
_PERIOD = date(2024, 12, 31)
_AVAILABLE = datetime(2025, 2, 14, tzinfo=UTC)


def _candidate(**updates: object) -> InstitutionalCompositionCandidate:
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
    return InstitutionalCompositionCandidate(**values)


def _resolve(*candidates: InstitutionalCompositionCandidate, known_at: datetime = _AVAILABLE):
    return resolve(
        manager_cik=_MANAGER,
        report_period=_PERIOD,
        known_at=known_at,
        candidates=tuple(candidates),
    )


def test_point_in_time_known_at_ignores_future_artifacts() -> None:
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


def test_ambiguous_available_at_tie_fails_closed() -> None:
    result = _resolve(_candidate(), _candidate(accession="0000950123-25-000002"))

    assert result.status == "ambiguous"
    assert result.reason == "available_at_tie"
    assert result.effective_artifact_id is None


def test_contradictory_amendment_chain_fails_closed() -> None:
    original = _candidate()
    first = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
    )
    duplicate = _candidate(
        accession="0000950123-25-000003",
        available_at=_AVAILABLE + timedelta(days=2),
        is_amendment=True,
        amendment_number="1",
        amendment_type="RESTATEMENT",
    )

    result = _resolve(original, first, duplicate, known_at=_AVAILABLE + timedelta(days=2))

    assert result.status == "ambiguous"
    assert result.reason == "contradictory_amendment_chain"
    assert result.effective_artifact_id is None


def test_unknown_amendment_type_is_not_defaulted() -> None:
    original = _candidate()
    amendment = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="UNDECLARED DEFAULT",
    )

    result = _resolve(original, amendment, known_at=_AVAILABLE + timedelta(days=1))

    assert (result.status, result.reason, result.effective_artifact_id) == (
        "ambiguous",
        "unknown_amendment_type",
        None,
    )


def test_absent_declared_total_is_missing_not_zero() -> None:
    result = _resolve(_candidate(declared_value_total=None))

    assert result.status == "not_evaluable"
    assert result.reason == "declared_total_missing"
    assert result.declared_value_total is None
    assert result.value_total_matches is None


def test_absent_rows_are_missing_not_zero() -> None:
    result = _resolve(_candidate(observed_entry_total=None, observed_value_total=None))

    assert result.status == "not_evaluable"
    assert result.reason == "observed_total_missing"
    assert result.observed_entry_total is None
    assert result.observed_value_total is None
    assert result.entry_total_matches is None
    assert result.value_total_matches is None


def test_insufficient_evidence_is_not_evaluable() -> None:
    result = _resolve(known_at=_AVAILABLE - timedelta(microseconds=1))

    assert (result.status, result.reason, result.effective_artifact_id) == (
        "insufficient",
        "no_visible_artifact",
        None,
    )


def test_missing_report_period_is_ambiguous_without_selection() -> None:
    result = resolve(
        manager_cik=_MANAGER,
        report_period=None,
        known_at=_AVAILABLE,
        candidates=(_candidate(report_period=None),),
    )

    assert (result.status, result.reason, result.effective_artifact_id) == (
        "ambiguous",
        "missing_or_conflicting_report_period",
        None,
    )


def test_no_effective_portfolio_composition_or_score_is_exposed() -> None:
    original = _candidate()
    amendment = _candidate(
        accession="0000950123-25-000002",
        available_at=_AVAILABLE + timedelta(days=1),
        is_amendment=True,
        amendment_number="1",
        amendment_type="NEW HOLDINGS ENTRIES",
    )

    result = _resolve(original, amendment, known_at=_AVAILABLE + timedelta(days=1))

    assert result.status == "amended"
    assert result.reason == "declared_amendment_new_holdings_entries"
    assert set(result.model_dump()) == {
        "manager_cik",
        "report_period",
        "known_at",
        "policy_version",
        "status",
        "reason",
        "effective_artifact_id",
        "effective_accession",
        "declared_entry_total",
        "observed_entry_total",
        "declared_value_total",
        "observed_value_total",
        "entry_total_matches",
        "value_total_matches",
    }
