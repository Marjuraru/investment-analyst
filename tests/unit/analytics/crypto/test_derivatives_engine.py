"""Exact Decimal34 vectors and negative analytical probes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import JsonValue

from investment_analyst.analytics.crypto.derivatives_engine import (
    ALGORITHM_VERSION,
    DVOL_CHANGE_KEY,
    FUNDING_MEAN_KEY,
    FUNDING_SUM_KEY,
    SPREAD_BPS_KEY,
    AmbiguousCryptoDerivativesRevisionError,
    CryptoDerivativesMetricEngine,
)
from investment_analyst.analytics.crypto.derivatives_identity import (
    diagnostic_id,
    metric_result_id,
    semantic_metric_result_id,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)

_ASSET = "crypto:btc-usd"
_FUNDING = "deribit:btc-perpetual:funding-rate-history"
_DVOL = "deribit:btc:dvol:daily"
_SUMMARY = "deribit:btc-perpetual:book-summary"
_START = datetime(2026, 6, 1, tzinfo=UTC)
_AVAILABLE = datetime(2026, 8, 1, tzinfo=UTC)
_KNOWN = datetime(2026, 8, 2, tzinfo=UTC)


def _identifier(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, name)


def _observation(
    field_name: str,
    observed_at: datetime,
    value: str,
    *,
    source_id: str,
    unit: str,
    frequency: DataFrequency,
    asset_id: str = _ASSET,
    available_at: datetime = _AVAILABLE,
    raw_record_id: UUID | None = None,
) -> NormalizedObservation:
    raw_id = raw_record_id or _identifier(
        f"raw:{asset_id}:{source_id}:{observed_at.isoformat()}:{value}"
    )
    return NormalizedObservation(
        observation_id=_identifier(f"obs:{raw_id}:{field_name}:{value}"),
        raw_record_id=raw_id,
        asset_id=asset_id,
        field_name=field_name,
        value=Decimal(value),
        unit=unit,
        frequency=frequency,
        observed_at=observed_at,
        period_start=(
            observed_at - timedelta(hours=1)
            if frequency is DataFrequency.HOUR_1
            else observed_at
            if frequency is DataFrequency.DAY_1
            else None
        ),
        period_end=(
            observed_at
            if frequency is DataFrequency.HOUR_1
            else observed_at + timedelta(days=1)
            if frequency is DataFrequency.DAY_1
            else None
        ),
        available_at=available_at,
        normalized_at=max(available_at, _KNOWN),
        source=SourceReference(source_id=source_id, retrieved_at=available_at),
        quality=DataQuality.VALID,
        transformation_version="test-v1",
    )


def _funding(count: int = 720) -> tuple[NormalizedObservation, ...]:
    return tuple(
        _observation(
            "funding_interest_1h",
            _START + timedelta(hours=index),
            "0.000001",
            source_id=_FUNDING,
            unit="ratio",
            frequency=DataFrequency.HOUR_1,
        )
        for index in range(count)
    )


def _dvol(count: int = 31) -> tuple[NormalizedObservation, ...]:
    return tuple(
        _observation(
            "dvol_close",
            _START + timedelta(days=index),
            str(50 + index),
            source_id=_DVOL,
            unit="dvol_index_points",
            frequency=DataFrequency.DAY_1,
        )
        for index in range(count)
    )


def _summary() -> tuple[NormalizedObservation, ...]:
    raw_id = _identifier("summary-raw")
    observed_at = _START + timedelta(days=30, hours=1)
    return tuple(
        _observation(
            field,
            observed_at,
            value,
            source_id=_SUMMARY,
            unit="USD",
            frequency=DataFrequency.EVENT,
            raw_record_id=raw_id,
        )
        for field, value in (("bid_price", "99"), ("ask_price", "101"), ("mid_price", "100"))
    )


def _compute(observations: tuple[NormalizedObservation, ...]):
    return CryptoDerivativesMetricEngine().compute(
        observations,
        asset_id=_ASSET,
        funding_source_id=_FUNDING,
        dvol_source_id=_DVOL,
        summary_source_id=_SUMMARY,
        known_at=_KNOWN,
        computed_at=_KNOWN + timedelta(seconds=1),
    )


def _latest(result, metric_key: str, window: int):
    return max(
        (
            item
            for item in result.results
            if item.metric_key == metric_key and item.parameters["window"] == window
        ),
        key=lambda item: item.as_of,
    )


def test_exact_funding_dvol_and_spread_vectors() -> None:
    result = _compute((*_funding(), *_dvol(), *_summary()))

    assert _latest(result, FUNDING_SUM_KEY, 24).value == Decimal("0.000024")
    assert _latest(result, FUNDING_MEAN_KEY, 24).value == Decimal("0.000001")
    assert _latest(result, FUNDING_SUM_KEY, 168).value == Decimal("0.000168")
    assert _latest(result, FUNDING_MEAN_KEY, 168).value == Decimal("0.000001")
    assert _latest(result, FUNDING_SUM_KEY, 720).value == Decimal("0.000720")
    assert _latest(result, FUNDING_MEAN_KEY, 720).value == Decimal("0.000001")
    assert _latest(result, DVOL_CHANGE_KEY, 1).value == Decimal("1")
    assert _latest(result, DVOL_CHANGE_KEY, 7).value == Decimal("7")
    assert _latest(result, DVOL_CHANGE_KEY, 30).value == Decimal("30")
    assert _latest(result, SPREAD_BPS_KEY, 1).value == Decimal("200")
    assert all(item.algorithm_version.endswith("decimal34") for item in result.results)
    assert all(item.available_at == _AVAILABLE for item in result.results)


def test_gaps_future_inputs_and_mixed_scope_do_not_create_invalid_metrics() -> None:
    funding = list(_funding(24))
    missing_time = funding.pop(10).observed_at
    future = _observation(
        "funding_interest_1h",
        missing_time,
        "0.000001",
        source_id=_FUNDING,
        unit="ratio",
        frequency=DataFrequency.HOUR_1,
        available_at=_KNOWN + timedelta(seconds=1),
    )
    mixed_source = _observation(
        "funding_interest_1h",
        missing_time,
        "0.000001",
        source_id="another:source",
        unit="ratio",
        frequency=DataFrequency.HOUR_1,
    )
    mixed_asset = _observation(
        "funding_interest_1h",
        missing_time,
        "0.000001",
        source_id=_FUNDING,
        unit="ratio",
        frequency=DataFrequency.HOUR_1,
        asset_id="crypto:eth-usd",
    )

    result = _compute((*funding, future, mixed_source, mixed_asset))

    assert not any(item.metric_key == FUNDING_SUM_KEY for item in result.results)
    assert "funding_interest_1h:consecutive_window=24" in result.missing_requirements


def test_spread_requires_same_raw_and_positive_mid() -> None:
    different_raw = tuple(
        _observation(
            field,
            _START,
            value,
            source_id=_SUMMARY,
            unit="USD",
            frequency=DataFrequency.EVENT,
        )
        for field, value in (("bid_price", "99"), ("ask_price", "101"), ("mid_price", "100"))
    )
    invalid_mid_raw = _identifier("invalid-mid")
    invalid_mid = tuple(
        _observation(
            field,
            _START + timedelta(hours=1),
            value,
            source_id=_SUMMARY,
            unit="USD",
            frequency=DataFrequency.EVENT,
            raw_record_id=invalid_mid_raw,
        )
        for field, value in (("bid_price", "0"), ("ask_price", "1"), ("mid_price", "0"))
    )

    result = _compute((*different_raw, *invalid_mid))

    assert not any(item.metric_key == SPREAD_BPS_KEY for item in result.results)


def test_semantically_different_equal_availability_revision_fails_closed() -> None:
    original = _funding(1)[0]
    conflicting = original.model_copy(
        update={
            "observation_id": _identifier("conflicting-revision"),
            "raw_record_id": _identifier("conflicting-raw"),
            "value": Decimal("0.000002"),
        }
    )

    with pytest.raises(AmbiguousCryptoDerivativesRevisionError):
        _compute((original, conflicting))


def test_engine_emits_v2_identities_without_known_at_and_with_unchanged_values() -> None:
    """A3: the four families emit semantic v2 identities with the same Decimal values."""
    observations = (*_funding(), *_dvol(), *_summary())
    engine = CryptoDerivativesMetricEngine()
    first = engine.compute(
        observations,
        asset_id=_ASSET,
        funding_source_id=_FUNDING,
        dvol_source_id=_DVOL,
        summary_source_id=_SUMMARY,
        known_at=_KNOWN,
        computed_at=_KNOWN + timedelta(seconds=1),
    )
    repeated = engine.compute(
        observations,
        asset_id=_ASSET,
        funding_source_id=_FUNDING,
        dvol_source_id=_DVOL,
        summary_source_id=_SUMMARY,
        known_at=_KNOWN,
        computed_at=_KNOWN + timedelta(days=1),
    )
    later_cut = engine.compute(
        observations,
        asset_id=_ASSET,
        funding_source_id=_FUNDING,
        dvol_source_id=_DVOL,
        summary_source_id=_SUMMARY,
        known_at=_KNOWN + timedelta(days=1),
        computed_at=_KNOWN + timedelta(days=1),
    )

    assert first.results
    assert all(item.result_id.version == 8 for item in first.results)
    assert all("known_at" not in item.parameters for item in first.results)
    assert all(item.parameters.get("source_ids") for item in first.results)
    assert all(
        "formula" in item.parameters and "window" in item.parameters for item in first.results
    )
    assert _latest(first, FUNDING_SUM_KEY, 24).value == Decimal("0.000024")
    assert _latest(first, FUNDING_MEAN_KEY, 168).value == Decimal("0.000001")
    assert _latest(first, DVOL_CHANGE_KEY, 7).value == Decimal("7")
    assert _latest(first, SPREAD_BPS_KEY, 1).value == Decimal("200")

    # computed_at stays out of the identity and a later cut reuses the same coordinates.
    assert [item.result_id for item in first.results] == [
        item.result_id for item in repeated.results
    ]
    assert [item.result_id for item in first.results] == [
        item.result_id for item in later_cut.results
    ]
    assert (
        _latest(first, FUNDING_SUM_KEY, 24).computed_at
        != _latest(repeated, FUNDING_SUM_KEY, 24).computed_at
    )


def test_v2_entrypoint_delegates_to_the_audited_rule_and_excludes_cut_value_and_clock() -> None:
    """A1: the v2 entry point consumes the audited rule and ignores value, cut and clock."""
    observation_ids = (_identifier("obs:a"), _identifier("obs:b"))
    parameters: dict[str, JsonValue] = {
        "formula": "sum(funding_interest_1h)",
        "known_at": _KNOWN.isoformat(),
        "source_ids": [_FUNDING],
        "window": 24,
    }
    coordinate: dict[str, object] = {
        "asset_id": _ASSET,
        "metric_key": FUNDING_SUM_KEY,
        "input_observation_ids": observation_ids,
        "parameters": parameters,
        "algorithm_version": ALGORITHM_VERSION,
        "as_of": _START + timedelta(hours=23),
        "available_at": _AVAILABLE,
        "value": Decimal("0.000024"),
        "unit": "ratio",
        "quality": DataQuality.VALID,
    }

    identifier = semantic_metric_result_id(
        **coordinate,
        known_at=_KNOWN,
        computed_at=_KNOWN + timedelta(seconds=1),
    )

    assert identifier == metric_result_id_v2(
        asset_id=_ASSET,
        metric_key=FUNDING_SUM_KEY,
        input_observation_ids=observation_ids,
        parameters=parameters,
        algorithm_version=ALGORITHM_VERSION,
        as_of=_START + timedelta(hours=23),
        available_at=_AVAILABLE,
        unit="ratio",
        quality=DataQuality.VALID,
    )
    assert identifier.version == 8
    assert str(identifier) == "a92ff801-55bd-8f91-be0b-9893625e69a5"
    assert identifier != metric_result_id(**coordinate)


def test_v1_metric_and_diagnostic_identities_are_unchanged() -> None:
    """A2: the legacy v1 metric and diagnostic identities stay exactly as they were."""
    observation_ids = (_identifier("obs:a"), _identifier("obs:b"))
    metric = metric_result_id(
        asset_id=_ASSET,
        metric_key=FUNDING_SUM_KEY,
        input_observation_ids=observation_ids,
        parameters={
            "formula": "sum(funding_interest_1h)",
            "known_at": _KNOWN.isoformat(),
            "source_ids": [_FUNDING],
            "window": 24,
        },
        algorithm_version=ALGORITHM_VERSION,
        as_of=_START + timedelta(hours=23),
        available_at=_AVAILABLE,
        value=Decimal("0.000024"),
        unit="ratio",
        quality=DataQuality.VALID,
    )
    diagnostic = diagnostic_id(
        asset_id=_ASSET,
        source_ids=(_DVOL, _SUMMARY, _FUNDING),
        known_at=_KNOWN,
        observation_ids=observation_ids,
        metric_result_ids=(metric,),
        dimensional_states={
            "dvol_direction": "unchanged",
            "funding_direction": "positive",
            "status": "complete",
        },
        algorithm_version="crypto-derivatives-diagnostic-v1",
    )

    assert str(metric) == "99809efc-172e-547c-837f-16d76177f1ca"
    assert metric.version == 5
    assert str(diagnostic) == "35242394-8bd9-5ea2-a57c-f83af8ca4d1a"
    assert diagnostic.version == 5


def test_derivatives_algorithm_version_is_unchanged() -> None:
    """X6: the identity adoption never changes the derivatives algorithm version."""
    result = _compute((*_funding(), *_dvol(), *_summary()))

    assert ALGORITHM_VERSION == "crypto-derivatives-metrics-v1-decimal34"
    assert {item.algorithm_version for item in result.results} == {ALGORITHM_VERSION}
