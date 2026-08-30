# ruff: noqa: E501
import importlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.activity_rule_engine import (
    evaluate_declared_activity_rules,
    evaluate_institutional_rules,
)
from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityFeatureSet,
    DeclaredActivityQueryResult,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import (
    DescriptiveMetric,
    InstitutionalChangeResult,
)

_AVAILABLE_AT = datetime(2025, 1, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2025, 2, 1, tzinfo=UTC)


def _insider_feature_set(**overrides: object) -> DeclaredActivityFeatureSet:
    base = dict(
        asset_id="equity:us:aapl",
        family="insider",
        participant_cik="0000000001",
        form="4",
        security_title="Common Stock",
        table="non_derivative",
        event_date=date(2025, 1, 1),
        available_at=_AVAILABLE_AT,
        revision_ids=("11111111-1111-1111-1111-111111111111",),
        comparison_status="available",
        metrics=(
            DescriptiveMetric(key="acquisition_count", status="available", value=Decimal("1")),
            DescriptiveMetric(key="disposition_count", status="missing"),
            DescriptiveMetric(key="holding_delta_ratio", status="not_evaluable"),
            DescriptiveMetric(
                key="clustered_transaction_count", status="available", value=Decimal("1")
            ),
            DescriptiveMetric(key="filing_delay_days", status="available", value=Decimal("5")),
        ),
    )
    base.update(overrides)
    return DeclaredActivityFeatureSet(**base)


def _beneficial_feature_set(**overrides: object) -> DeclaredActivityFeatureSet:
    base = dict(
        asset_id="equity:us:aapl",
        family="beneficial_ownership",
        participant_cik="0000000002",
        form="SC 13D",
        declared_nature="SC 13D",
        event_date=date(2025, 1, 1),
        available_at=_AVAILABLE_AT,
        revision_ids=("22222222-2222-2222-2222-222222222222",),
        comparison_status="available",
        metrics=(
            DescriptiveMetric(key="threshold_appearance", status="available", value=True),
            DescriptiveMetric(key="threshold_exit", status="missing"),
            DescriptiveMetric(
                key="delta_percent_of_class", status="available", value=Decimal("2.5")
            ),
        ),
    )
    base.update(overrides)
    return DeclaredActivityFeatureSet(**base)


def test_insider_rules_produce_met_not_met_and_not_evaluable() -> None:
    query_result = DeclaredActivityQueryResult(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        insider_features=(_insider_feature_set(),),
        beneficial_features=(),
        total_statements=1,
    )
    result = evaluate_declared_activity_rules(query_result)
    by_rule = {evaluation.rule_id: evaluation for evaluation in result.evaluations}

    assert by_rule["insider-acquisition-declared"].status == "met"
    assert by_rule["insider-disposition-declared"].status == "not_evaluable"
    assert by_rule["insider-disposition-declared"].feature_status == "missing"
    assert by_rule["insider-holding-reduced"].status == "not_evaluable"
    assert by_rule["insider-holding-reduced"].feature_status == "not_evaluable"
    assert by_rule["insider-clustered-activity"].status == "not_met"
    assert by_rule["insider-filing-delay-beyond-threshold"].status == "met"
    assert result.total_features_evaluated == len(result.evaluations)


def test_beneficial_rules_produce_met_not_met_and_not_evaluable() -> None:
    query_result = DeclaredActivityQueryResult(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        insider_features=(),
        beneficial_features=(_beneficial_feature_set(),),
        total_statements=1,
    )
    result = evaluate_declared_activity_rules(query_result)
    by_rule = {evaluation.rule_id: evaluation for evaluation in result.evaluations}

    assert by_rule["beneficial-threshold-appearance"].status == "met"
    assert by_rule["beneficial-threshold-exit"].status == "not_evaluable"
    assert by_rule["beneficial-percent-increase"].status == "met"
    assert by_rule["beneficial-percent-decrease"].status == "not_met"


def test_insider_and_beneficial_rules_never_cross_families() -> None:
    query_result = DeclaredActivityQueryResult(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        insider_features=(_insider_feature_set(),),
        beneficial_features=(_beneficial_feature_set(),),
        total_statements=2,
    )
    result = evaluate_declared_activity_rules(query_result)
    for evaluation in result.evaluations:
        if evaluation.subject_id == "equity:us:aapl" and evaluation.participant_id == "0000000001":
            assert evaluation.family == "insider"
        if evaluation.participant_id == "0000000002":
            assert evaluation.family == "beneficial_ownership"


def _institutional_metric(key: str, *, available: bool, value: Decimal | bool | None = None):
    return DescriptiveMetric(
        key=key, status="available" if available else "missing", value=value if available else None
    )


def _institutional_result(metrics: tuple[DescriptiveMetric, ...]) -> InstitutionalChangeResult:
    return InstitutionalChangeResult(
        manager_cik="0001067983",
        previous_period=date(2024, 9, 30),
        current_period=date(2024, 12, 31),
        available_at=_AVAILABLE_AT,
        metrics=metrics,
    )


def test_institutional_entry_exit_are_reduced_existentially_across_positions() -> None:
    result = _institutional_result(
        (
            _institutional_metric("entry", available=True, value=True),
            _institutional_metric("entry", available=True, value=False),
            _institutional_metric("exit", available=True, value=False),
            _institutional_metric("exit", available=True, value=False),
            _institutional_metric("position_concentration", available=True, value=Decimal("0.5")),
        )
    )
    evaluations = evaluate_institutional_rules((result,))
    by_rule = {evaluation.rule_id: evaluation for evaluation in evaluations.evaluations}

    assert by_rule["institutional-position-entry"].status == "met"
    assert by_rule["institutional-position-exit"].status == "not_met"
    assert by_rule["institutional-position-concentration"].status == "met"
    assert by_rule["institutional-robust-percentile-high"].status == "not_evaluable"
    assert "minimum" in by_rule["institutional-robust-percentile-high"].reason


def test_institutional_singleton_feature_repeated_fails_closed() -> None:
    result = _institutional_result(
        (
            _institutional_metric("position_concentration", available=True, value=Decimal("0.1")),
            _institutional_metric("position_concentration", available=True, value=Decimal("0.2")),
        )
    )
    with pytest.raises(ValueError, match="unexpectedly repeated"):
        evaluate_institutional_rules((result,))


def test_institutional_missing_declared_value_total_is_not_evaluable() -> None:
    result = _institutional_result(
        (_institutional_metric("position_concentration", available=False),)
    )
    evaluations = evaluate_institutional_rules((result,))
    by_rule = {evaluation.rule_id: evaluation for evaluation in evaluations.evaluations}
    assert by_rule["institutional-position-concentration"].status == "not_evaluable"
    assert by_rule["institutional-position-concentration"].feature_status == "missing"


def test_evaluations_are_deterministic_for_identical_evidence() -> None:
    query_result = DeclaredActivityQueryResult(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        insider_features=(_insider_feature_set(),),
        beneficial_features=(_beneficial_feature_set(),),
        total_statements=2,
    )
    first = evaluate_declared_activity_rules(query_result)
    second = evaluate_declared_activity_rules(query_result)
    assert first == second


def test_activity_rule_modules_never_import_forbidden_surfaces() -> None:
    forbidden_substrings = (
        "investment_analyst.alerts",
        "investment_analyst.analytics.market",
        "investment_analyst.analytics.valuation",
        "investment_analyst.analytics.fundamentals",
        "investment_analyst.analytics.crypto",
        "investment_analyst.analytics.unified",
        "instrument_correspondence",
    )
    modules = (
        "investment_analyst.analytics.cazatiburones.activity_rule_models",
        "investment_analyst.analytics.cazatiburones.activity_rule_definitions",
        "investment_analyst.analytics.cazatiburones.activity_rule_engine",
        "investment_analyst.analytics.cazatiburones.activity_rule_service",
        "investment_analyst.application.cazatiburones_activity_rules",
    )
    for module_name in modules:
        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in forbidden_substrings:
            assert forbidden not in source, f"{module_name} references forbidden '{forbidden}'"


def test_uuid_looking_revision_ids_are_preserved_verbatim() -> None:
    feature_set = _insider_feature_set(revision_ids=(str(uuid4()),))
    query_result = DeclaredActivityQueryResult(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        insider_features=(feature_set,),
        beneficial_features=(),
        total_statements=1,
    )
    result = evaluate_declared_activity_rules(query_result)
    assert all(
        evaluation.revision_ids == feature_set.revision_ids for evaluation in result.evaluations
    )
