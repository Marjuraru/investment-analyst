"""Tests for independent deterministic market and fundamental screening."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.alerts.analytical_engine import (
    AmbiguousAnalyticalMetricError,
    AnalyticalScreeningEngine,
    IncompatibleAnalyticalMetricError,
)
from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionOperator,
    AnalyticalConditionResult,
    AnalyticalConditionState,
    AnalyticalRuleState,
    AnalyticalScreeningCondition,
    AnalyticalScreeningDomain,
    AnalyticalScreeningRequest,
    AnalyticalScreeningRule,
)
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    INITIAL_MARKET_ACTIVITY_RULE,
    INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
)
from investment_analyst.core.models import AssetClass, DataQuality, MetricResult

_AS_OF = datetime(2026, 6, 30, tzinfo=UTC)
_AVAILABLE_AT = datetime(2026, 7, 29, 15, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 7, 29, 16, tzinfo=UTC)
_COMPUTED_AT = datetime(2026, 7, 29, 16, 5, tzinfo=UTC)
_MARKET_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_FUNDAMENTAL_SOURCE_ID = "sec-edgar:aapl:companyfacts"


def _metric(
    *,
    identifier: int,
    metric_key: str,
    value: str,
    algorithm_version: str,
    source_id: str,
    parameters: dict[str, object] | None = None,
    quality: DataQuality = DataQuality.VALID,
    unit: str = "ratio",
    asset_id: str = "equity:us:aapl",
    as_of: datetime = _AS_OF,
    available_at: datetime = _AVAILABLE_AT,
) -> MetricResult:
    return MetricResult(
        result_id=UUID(f"00000000-0000-4000-8000-{identifier:012d}"),
        asset_id=asset_id,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=max(available_at, _KNOWN_AT),
        parameters={"source_id": source_id, **(parameters or {})},
        input_observation_ids=[UUID(f"10000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=algorithm_version,
        quality=quality,
    )


def _market_metric(
    value: str,
    *,
    identifier: int = 1,
    quality: DataQuality = DataQuality.PARTIAL,
) -> MetricResult:
    return _metric(
        identifier=identifier,
        metric_key="market.history.relative_volume",
        value=value,
        algorithm_version="market-relative-volume-v1-decimal34",
        source_id=_MARKET_SOURCE_ID,
        parameters={"window": 20},
        quality=quality,
    )


def _fundamental_metrics(
    *,
    liabilities_to_assets: str = "0.45",
    net_margin: str = "0.18",
    revenue_growth: str = "0.07",
) -> tuple[MetricResult, ...]:
    return (
        _metric(
            identifier=10,
            metric_key="fundamental.liabilities_to_assets",
            value=liabilities_to_assets,
            algorithm_version="sec-fundamental-liabilities-to-assets-v1-decimal34",
            source_id=_FUNDAMENTAL_SOURCE_ID,
            parameters={"frequency": "quarterly"},
        ),
        _metric(
            identifier=11,
            metric_key="fundamental.net_margin",
            value=net_margin,
            algorithm_version="sec-fundamental-net-margin-v1-decimal34",
            source_id=_FUNDAMENTAL_SOURCE_ID,
            parameters={"frequency": "quarterly"},
        ),
        _metric(
            identifier=12,
            metric_key="fundamental.revenue_yoy_growth",
            value=revenue_growth,
            algorithm_version="sec-fundamental-revenue-yoy-growth-v1-decimal34",
            source_id=_FUNDAMENTAL_SOURCE_ID,
            parameters={"frequency": "quarterly"},
        ),
    )


def _request(
    rule: AnalyticalScreeningRule,
    metrics: tuple[MetricResult, ...],
    *,
    source_id: str,
    asset_class: AssetClass = AssetClass.EQUITY,
    computed_at: datetime = _COMPUTED_AT,
) -> AnalyticalScreeningRequest:
    return AnalyticalScreeningRequest(
        rule=rule,
        asset_id="equity:us:aapl",
        asset_class=asset_class,
        source_id=source_id,
        known_at=_KNOWN_AT,
        computed_at=computed_at,
        metrics=metrics,
    )


def _matched_market_result():
    return AnalyticalScreeningEngine().evaluate(
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (_market_metric("1.75"),),
            source_id=_MARKET_SOURCE_ID,
        )
    )


def test_initial_rule_catalog_is_ordered_silent_and_domain_separated() -> None:
    assert tuple(item.rule_id for item in INITIAL_ANALYTICAL_RULES) == tuple(
        sorted(item.rule_id for item in INITIAL_ANALYTICAL_RULES)
    )
    assert all(item.state is AnalyticalRuleState.SILENT for item in INITIAL_ANALYTICAL_RULES)
    assert {item.domain for item in INITIAL_ANALYTICAL_RULES} == {
        AnalyticalScreeningDomain.MARKET,
        AnalyticalScreeningDomain.FUNDAMENTALS,
    }
    assert all(
        all(condition.domain is rule.domain for condition in rule.conditions)
        for rule in INITIAL_ANALYTICAL_RULES
    )
    assert INITIAL_MARKET_ACTIVITY_RULE.confirmations_required == 2
    assert INITIAL_MARKET_ACTIVITY_RULE.cooldown_seconds == 86_400
    assert INITIAL_MARKET_ACTIVITY_RULE.conditions[0].retention_threshold == Decimal("1.2")
    assert INITIAL_QUARTERLY_FUNDAMENTAL_RULE.confirmations_required == 1
    assert INITIAL_QUARTERLY_FUNDAMENTAL_RULE.cooldown_seconds == 604_800


def test_market_rule_activates_silently_with_partial_iex_evidence_and_stable_identity() -> None:
    engine = AnalyticalScreeningEngine()
    metric = _market_metric("1.75")
    first = engine.evaluate(
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (metric,),
            source_id=_MARKET_SOURCE_ID,
        )
    )
    repeated = engine.evaluate(
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (metric,),
            source_id=_MARKET_SOURCE_ID,
            computed_at=_COMPUTED_AT + timedelta(hours=1),
        )
    )

    assert first.result_id == repeated.result_id
    assert first.computed_at != repeated.computed_at
    assert first.matched is True
    assert first.retained is True
    assert first.activated is True
    assert first.as_of == _AS_OF
    assert first.conditions[0].state is AnalyticalConditionState.MET
    assert first.conditions[0].quality is DataQuality.PARTIAL
    assert first.conditions[0].metric_result_id == metric.result_id
    assert "no constituye una recomendación" in first.explanation_es


def test_draft_rule_can_match_without_activating_a_candidate() -> None:
    draft = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"state": AnalyticalRuleState.DRAFT})

    result = AnalyticalScreeningEngine().evaluate(
        _request(draft, (_market_metric("2"),), source_id=_MARKET_SOURCE_ID)
    )

    assert result.matched is True
    assert result.activated is False


def test_fundamental_rule_keeps_conditions_independent_and_requires_all() -> None:
    engine = AnalyticalScreeningEngine()
    matched = engine.evaluate(
        _request(
            INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
            _fundamental_metrics(),
            source_id=_FUNDAMENTAL_SOURCE_ID,
        )
    )
    not_matched = engine.evaluate(
        _request(
            INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
            _fundamental_metrics(revenue_growth="-0.02"),
            source_id=_FUNDAMENTAL_SOURCE_ID,
        )
    )

    assert tuple(item.state for item in matched.conditions) == (
        AnalyticalConditionState.MET,
        AnalyticalConditionState.MET,
        AnalyticalConditionState.MET,
    )
    assert matched.matched is True
    assert matched.activated is True
    assert tuple(item.state for item in not_matched.conditions) == (
        AnalyticalConditionState.MET,
        AnalyticalConditionState.MET,
        AnalyticalConditionState.NOT_MET,
    )
    assert not_matched.matched is False
    assert not_matched.activated is False
    assert "no se activa un candidato" in not_matched.explanation_es


def test_missing_or_unaccepted_evidence_is_not_evaluable_never_zero() -> None:
    metrics = _fundamental_metrics()[:2]
    result = AnalyticalScreeningEngine().evaluate(
        _request(
            INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
            metrics,
            source_id=_FUNDAMENTAL_SOURCE_ID,
        )
    )
    no_metrics = AnalyticalScreeningEngine().evaluate(
        _request(
            INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
            (),
            source_id=_FUNDAMENTAL_SOURCE_ID,
        )
    )

    assert result.conditions[-1].state is AnalyticalConditionState.NOT_EVALUABLE
    assert result.conditions[-1].observed_value is None
    assert result.as_of == _AS_OF
    assert result.matched is False
    assert no_metrics.as_of is None
    assert all(
        item.state is AnalyticalConditionState.NOT_EVALUABLE for item in no_metrics.conditions
    )


def test_request_rejects_mixed_context_and_future_evidence() -> None:
    metric = _market_metric("1.75")
    with pytest.raises(ValidationError, match="another asset"):
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (metric.model_copy(update={"asset_id": "equity:us:amd"}),),
            source_id=_MARKET_SOURCE_ID,
        )
    with pytest.raises(ValidationError, match="source_id"):
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (metric,),
            source_id="other-source",
        )
    with pytest.raises(ValidationError, match="unavailable at known_at"):
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (
                metric.model_copy(
                    update={
                        "available_at": _KNOWN_AT + timedelta(seconds=1),
                        "computed_at": _KNOWN_AT + timedelta(seconds=2),
                    }
                ),
            ),
            source_id=_MARKET_SOURCE_ID,
        )
    with pytest.raises(ValidationError, match="common as_of"):
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (
                metric,
                _market_metric(
                    "2",
                    identifier=2,
                ).model_copy(update={"as_of": _AS_OF + timedelta(days=1)}),
            ),
            source_id=_MARKET_SOURCE_ID,
        )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("algorithm_version", "future-algorithm", "algorithm version"),
        ("unit", "USD", "unit"),
    ),
)
def test_engine_rejects_incompatible_metric_contracts(
    field: str,
    replacement: str,
    message: str,
) -> None:
    metric = _market_metric("1.75").model_copy(update={field: replacement})

    with pytest.raises(IncompatibleAnalyticalMetricError, match=message):
        AnalyticalScreeningEngine().evaluate(
            _request(
                INITIAL_MARKET_ACTIVITY_RULE,
                (metric,),
                source_id=_MARKET_SOURCE_ID,
            )
        )


def test_engine_rejects_ambiguous_compatible_revisions() -> None:
    with pytest.raises(AmbiguousAnalyticalMetricError, match="multiple compatible"):
        AnalyticalScreeningEngine().evaluate(
            _request(
                INITIAL_MARKET_ACTIVITY_RULE,
                (_market_metric("1.75"), _market_metric("1.80", identifier=2)),
                source_id=_MARKET_SOURCE_ID,
            )
        )


def test_parameter_or_quality_mismatch_is_not_evaluable() -> None:
    wrong_window = _market_metric("1.75").model_copy(
        update={
            "parameters": {
                "source_id": _MARKET_SOURCE_ID,
                "window": 50,
            }
        }
    )
    suspect = _market_metric("1.75").model_copy(update={"quality": DataQuality.SUSPECT})

    for metric in (wrong_window, suspect):
        result = AnalyticalScreeningEngine().evaluate(
            _request(
                INITIAL_MARKET_ACTIVITY_RULE,
                (metric,),
                source_id=_MARKET_SOURCE_ID,
            )
        )
        assert result.conditions[0].state is AnalyticalConditionState.NOT_EVALUABLE


@pytest.mark.parametrize(
    ("operator", "value", "threshold", "expected"),
    (
        (AnalyticalConditionOperator.GREATER_THAN, "2", "1", True),
        (AnalyticalConditionOperator.GREATER_THAN_OR_EQUAL, "1", "1", True),
        (AnalyticalConditionOperator.LESS_THAN, "0", "1", True),
        (AnalyticalConditionOperator.LESS_THAN_OR_EQUAL, "1", "1", True),
        (AnalyticalConditionOperator.EQUAL, "1", "1", True),
        (AnalyticalConditionOperator.EQUAL, "1.1", "1", False),
    ),
)
def test_all_decimal_operators_are_explicit(
    operator: AnalyticalConditionOperator,
    value: str,
    threshold: str,
    expected: bool,
) -> None:
    assert operator.compare(Decimal(value), Decimal(threshold)) is expected


def test_condition_and_rule_models_reject_nondeterministic_configuration() -> None:
    base = INITIAL_MARKET_ACTIVITY_RULE.conditions[0].model_dump(mode="python")
    for update, message in (
        ({"threshold": "NaN"}, "finite"),
        ({"exit_threshold": "NaN"}, "finite"),
        ({"exit_threshold": "1.6"}, "must not exceed"),
        ({"accepted_qualities": ()}, "must not be empty"),
        (
            {"accepted_qualities": (DataQuality.VALID, DataQuality.PARTIAL)},
            "unique and sorted",
        ),
        ({"parameter_filters": {"window": float("nan")}}, "JSON serializable"),
    ):
        with pytest.raises(ValidationError, match=message):
            AnalyticalScreeningCondition.model_validate({**base, **update})

    rule = INITIAL_MARKET_ACTIVITY_RULE.model_dump(mode="python")
    for update, message in (
        ({"confirmations_required": True}, "must be integers"),
        ({"cooldown_seconds": False}, "must be integers"),
        ({"asset_classes": ()}, "must not be empty"),
        (
            {"asset_classes": (AssetClass.EQUITY, AssetClass.CRYPTO)},
            "unique and sorted",
        ),
        ({"conditions": ()}, "conditions must not be empty"),
        (
            {
                "conditions": (
                    INITIAL_MARKET_ACTIVITY_RULE.conditions[0],
                    INITIAL_MARKET_ACTIVITY_RULE.conditions[0],
                )
            },
            "unique and sorted",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            AnalyticalScreeningRule.model_validate({**rule, **update})


def test_request_rejects_incompatible_class_time_and_duplicate_metric_identity() -> None:
    metric = _market_metric("1.75")
    with pytest.raises(ValidationError, match="asset_class"):
        _request(
            INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
            (),
            source_id=_FUNDAMENTAL_SOURCE_ID,
            asset_class=AssetClass.ETF,
        )
    with pytest.raises(ValidationError, match="predate"):
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (metric,),
            source_id=_MARKET_SOURCE_ID,
            computed_at=_KNOWN_AT - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="result IDs must be unique"):
        _request(
            INITIAL_MARKET_ACTIVITY_RULE,
            (metric, metric),
            source_id=_MARKET_SOURCE_ID,
        )


def test_condition_result_rejects_incomplete_or_contradictory_evidence() -> None:
    condition = _matched_market_result().conditions[0]
    payload = condition.model_dump(mode="python")
    with pytest.raises(ValidationError, match="cannot claim"):
        AnalyticalConditionResult.model_validate(
            {
                **payload,
                "state": AnalyticalConditionState.NOT_EVALUABLE,
                "retention_state": AnalyticalConditionState.NOT_EVALUABLE,
            }
        )
    with pytest.raises(ValidationError, match="complete metric evidence"):
        AnalyticalConditionResult.model_validate(
            {
                **payload,
                "metric_result_id": None,
            }
        )
    with pytest.raises(ValidationError, match="finite"):
        AnalyticalConditionResult.model_validate(
            {
                **payload,
                "observed_value": "NaN",
            }
        )
    with pytest.raises(ValidationError, match="exact comparison"):
        AnalyticalConditionResult.model_validate(
            {
                **payload,
                "state": AnalyticalConditionState.NOT_MET,
            }
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        ({"computed_at": _KNOWN_AT - timedelta(seconds=1)}, "predate"),
        ({"conditions": ()}, "rule order"),
        ({"as_of": None}, "without as_of"),
        ({"as_of": _AS_OF + timedelta(days=1)}, "match the result as_of"),
        ({"matched": False}, "matched must require"),
        ({"retained": False}, "retained must match"),
        ({"activated": False}, "activated must match"),
        (
            {"result_id": UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")},
            "not deterministic",
        ),
    ),
)
def test_screening_result_rejects_tampered_derived_fields(
    mutate: dict[str, object],
    message: str,
) -> None:
    result = _matched_market_result()
    with pytest.raises(ValidationError, match=message):
        type(result).model_validate(
            {
                **result.model_dump(mode="python"),
                **mutate,
            }
        )


def test_screening_result_rejects_incompatible_asset_and_future_condition_evidence() -> None:
    result = _matched_market_result()
    incompatible_rule = result.rule.model_copy(update={"asset_classes": (AssetClass.ETF,)})
    with pytest.raises(ValidationError, match="asset_class"):
        type(result).model_validate(
            {
                **result.model_dump(mode="python"),
                "rule": incompatible_rule,
            }
        )
    future_condition = result.conditions[0].model_copy(
        update={"available_at": _KNOWN_AT + timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match="unavailable at known_at"):
        type(result).model_validate(
            {
                **result.model_dump(mode="python"),
                "conditions": (future_condition,),
            }
        )
    assert result.to_json_dict()["result_id"] == str(result.result_id)


def test_rule_contract_rejects_float_thresholds_and_cross_domain_conditions() -> None:
    with pytest.raises(ValidationError, match="must use Decimal"):
        AnalyticalScreeningCondition(
            condition_id="invalid_float",
            label_es="Umbral inválido",
            domain=AnalyticalScreeningDomain.MARKET,
            metric_key="market.test",
            algorithm_version="test-v1",
            unit="ratio",
            operator=AnalyticalConditionOperator.GREATER_THAN,
            threshold=0.5,
        )
    market_condition = INITIAL_MARKET_ACTIVITY_RULE.conditions[0]
    with pytest.raises(ValidationError, match="rule domain"):
        AnalyticalScreeningRule(
            rule_id="fundamentals.invalid",
            rule_version="1.0",
            name_es="Inválida",
            description_es="Mezcla dominios incompatibles.",
            state=AnalyticalRuleState.DRAFT,
            domain=AnalyticalScreeningDomain.FUNDAMENTALS,
            asset_classes=(AssetClass.EQUITY,),
            conditions=(market_condition,),
        )
