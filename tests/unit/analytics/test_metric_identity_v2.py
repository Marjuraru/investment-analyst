"""Unit tests for canonical metric identity v2 and resolution adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investment_analyst.analytics.crypto import derivatives_identity
from investment_analyst.analytics.market import statistics_identity
from investment_analyst.analytics.market.statistics_models import MetricCalculation
from investment_analyst.analytics.metric_identity_v2 import (
    EXECUTION_PARAMETER_KEYS,
    MetricIdentityVersion,
    canonical_preimage_dict_v2,
    canonical_preimage_v2,
    filter_semantic_parameters,
    is_execution_parameter,
    metric_result_id_from_model_v2,
    metric_result_id_v2,
    resolve_identity_version,
    resolve_metric_identity_version,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult

_ASSET_ID = "equity:us:aapl"
_METRIC_KEY = "close_price"
_AS_OF = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)
_AVAILABLE_AT = datetime(2026, 9, 16, 20, 5, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)
_COMPUTED_AT = datetime(2026, 9, 16, 21, 5, tzinfo=UTC)
_ALGORITHM_VERSION = "statistics:v1"
_UNIT = "USD"
_QUALITY = DataQuality.VALID
_OBS_ID_1 = UUID("11111111-1111-4111-8111-111111111111")
_OBS_ID_2 = UUID("22222222-2222-4222-8222-222222222222")
_DEP_ID_1 = UUID("33333333-3333-4333-8333-333333333333")


def _sample_coordinates() -> dict[str, object]:
    return {
        "asset_id": _ASSET_ID,
        "metric_key": _METRIC_KEY,
        "input_observation_ids": [_OBS_ID_2, _OBS_ID_1],
        "input_metric_result_ids": [_DEP_ID_1],
        "algorithm_version": _ALGORITHM_VERSION,
        "as_of": _AS_OF,
        "available_at": _AVAILABLE_AT,
        "unit": _UNIT,
        "quality": _QUALITY,
        "parameters": {"window": 20, "formula": "sma"},
    }


def test_v2_preimage_contains_exactly_the_semantic_coordinates() -> None:
    """A1: Preimage contains exactly the declared 10 semantic coordinates."""
    coords = _sample_coordinates()
    preimage = canonical_preimage_dict_v2(**coords)

    expected_keys = {
        "algorithm_version",
        "as_of",
        "asset_id",
        "available_at",
        "input_metric_result_ids",
        "input_observation_ids",
        "metric_key",
        "parameters",
        "quality",
        "unit",
    }
    assert set(preimage.keys()) == expected_keys
    assert preimage["asset_id"] == _ASSET_ID
    assert preimage["metric_key"] == _METRIC_KEY
    assert preimage["algorithm_version"] == _ALGORITHM_VERSION
    assert preimage["as_of"] == _AS_OF.isoformat()
    assert preimage["available_at"] == _AVAILABLE_AT.isoformat()
    assert preimage["unit"] == _UNIT
    assert preimage["quality"] == _QUALITY.value
    assert preimage["parameters"] == {"formula": "sma", "window": 20}
    # Deterministically sorted observation IDs
    assert preimage["input_observation_ids"] == sorted([str(_OBS_ID_1), str(_OBS_ID_2)])
    assert preimage["input_metric_result_ids"] == [str(_DEP_ID_1)]


def test_v2_preimage_excludes_known_at_computed_at_value_and_job_identifiers() -> None:
    """A2: Preimage strictly excludes known_at, computed_at, value, execution params, job IDs."""
    coords = _sample_coordinates()
    # Pass known_at, computed_at, value as top-level kwargs AND injected in parameters
    coords_with_execution = {
        **coords,
        "value": Decimal("150.25"),
        "known_at": _KNOWN_AT,
        "computed_at": _COMPUTED_AT,
        "job_id": "job-market-stats-01",
        "attempt_id": "attempt-001",
        "parameters": {
            "window": 20,
            "formula": "sma",
            "known_at": _KNOWN_AT.isoformat(),
            "computed_at": _COMPUTED_AT.isoformat(),
            "value": "150.25",
            "job_id": "job-market-stats-01",
            "attempt_id": "attempt-001",
            "run_id": "run-42",
            "worker_id": "worker-1",
            "execution_id": "exec-abc",
            "batch_id": "batch-xyz",
            "trace_id": "trace-999",
            "requested_cut": "2026-09-16T21:00:00Z",
            "clock": "system-clock",
        },
    }

    preimage = canonical_preimage_dict_v2(**coords_with_execution)
    for forbidden in (
        "known_at",
        "computed_at",
        "value",
        "job_id",
        "attempt_id",
        "run_id",
        "worker_id",
        "execution_id",
        "batch_id",
        "trace_id",
        "requested_cut",
        "clock",
    ):
        assert forbidden not in preimage
        assert forbidden not in preimage["parameters"]

    # Changing any of these execution coordinates must not alter the resulting preimage or UUID
    id_clean = metric_result_id_v2(**coords)
    id_with_exec = metric_result_id_v2(**coords_with_execution)
    assert id_clean == id_with_exec
    assert canonical_preimage_v2(**coords) == canonical_preimage_v2(**coords_with_execution)


def test_same_semantic_inputs_always_produce_the_same_identity() -> None:
    """A3: Determinism: identical semantic inputs yield identical UUIDs."""
    coords = _sample_coordinates()
    id_1 = metric_result_id_v2(**coords)
    id_2 = metric_result_id_v2(**coords)
    assert id_1 == id_2
    assert str(id_1) == str(id_2)


def test_a_different_value_keeps_the_same_identity_so_it_surfaces_as_conflict() -> None:
    """A4: Different computed value keeps same ID, exposing non-determinism as conflict."""
    coords = _sample_coordinates()
    id_val_1 = metric_result_id_v2(**coords, value=Decimal("150.25"))
    id_val_2 = metric_result_id_v2(**coords, value=Decimal("999.99"))
    id_no_val = metric_result_id_v2(**coords)

    assert id_val_1 == id_val_2
    assert id_val_1 == id_no_val

    # Construct two MetricResults with same semantic coordinates but different values
    res_1 = MetricResult(
        result_id=id_val_1,
        asset_id=_ASSET_ID,
        metric_key=_METRIC_KEY,
        value=Decimal("150.25"),
        unit=_UNIT,
        as_of=_AS_OF,
        available_at=_AVAILABLE_AT,
        computed_at=_COMPUTED_AT,
        parameters={"window": 20, "formula": "sma"},
        input_observation_ids=[_OBS_ID_1],
        algorithm_version=_ALGORITHM_VERSION,
        quality=_QUALITY,
    )
    res_2 = MetricResult(
        result_id=id_val_2,
        asset_id=_ASSET_ID,
        metric_key=_METRIC_KEY,
        value=Decimal("999.99"),
        unit=_UNIT,
        as_of=_AS_OF,
        available_at=_AVAILABLE_AT,
        computed_at=_COMPUTED_AT,
        parameters={"window": 20, "formula": "sma"},
        input_observation_ids=[_OBS_ID_1],
        algorithm_version=_ALGORITHM_VERSION,
        quality=_QUALITY,
    )
    assert metric_result_id_from_model_v2(res_1) == metric_result_id_from_model_v2(res_2)


def test_new_or_revised_evidence_produces_a_different_identity() -> None:
    """A5: New or revised evidence changes the semantic identity."""
    base_coords = _sample_coordinates()
    base_id = metric_result_id_v2(**base_coords)

    # 1. New observation in evidence set
    new_obs = {**base_coords, "input_observation_ids": [_OBS_ID_1, uuid4()]}
    assert metric_result_id_v2(**new_obs) != base_id

    # 2. Revised available_at
    revised_avail = {
        **base_coords,
        "available_at": _AVAILABLE_AT + timedelta(minutes=10),
    }
    assert metric_result_id_v2(**revised_avail) != base_id

    # 3. New as_of point-in-time
    new_as_of = {**base_coords, "as_of": _AS_OF + timedelta(days=1)}
    assert metric_result_id_v2(**new_as_of) != base_id

    # 4. Changed metric dependency
    new_dep = {**base_coords, "input_metric_result_ids": [uuid4()]}
    assert metric_result_id_v2(**new_dep) != base_id


def test_v2_identity_space_is_disjoint_from_v1_and_reassigns_nothing() -> None:
    """A6: v2 space (UUIDv8) is disjoint from v1 space (UUIDv5) by construction."""
    coords = _sample_coordinates()
    v2_id = metric_result_id_v2(**coords)

    # v2 identity is RFC 9562 UUIDv8
    assert v2_id.version == 8
    assert v2_id.variant == "specified in RFC 4122"

    # v1 identities are RFC 4122 UUIDv5
    calc = MetricCalculation(
        asset_id=_ASSET_ID,
        source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        metric_key=_METRIC_KEY,
        as_of=_AS_OF,
        available_at=_AVAILABLE_AT,
        parameters={"window": 20},
        input_observation_ids=[_OBS_ID_1],
        algorithm_version=_ALGORITHM_VERSION,
        value=Decimal("150.25"),
        unit=_UNIT,
        quality=_QUALITY,
    )
    v1_market_id = statistics_identity.metric_result_id(calc, _KNOWN_AT)
    assert v1_market_id.version == 5

    v1_derivatives_id = derivatives_identity.metric_result_id(
        asset_id=_ASSET_ID,
        metric_key=_METRIC_KEY,
        input_observation_ids=(_OBS_ID_1,),
        parameters={"window": 20},
        algorithm_version=_ALGORITHM_VERSION,
        as_of=_AS_OF,
        available_at=_AVAILABLE_AT,
        value=Decimal("150.25"),
        unit=_UNIT,
        quality=_QUALITY,
    )
    assert v1_derivatives_id.version == 5

    # Because version 8 != version 5, no v1 ID can equal any v2 ID
    assert v2_id != v1_market_id
    assert v2_id != v1_derivatives_id
    assert {v2_id}.isdisjoint({v1_market_id, v1_derivatives_id})


def test_adapter_resolves_identity_version_without_reading_storage() -> None:
    """A7: Adapter resolves v1 vs v2 without reading storage or performing I/O."""
    coords = _sample_coordinates()
    v2_id = metric_result_id_v2(**coords)

    calc = MetricCalculation(
        asset_id=_ASSET_ID,
        source_id="source-1",
        metric_key=_METRIC_KEY,
        as_of=_AS_OF,
        available_at=_AVAILABLE_AT,
        parameters={"window": 20},
        input_observation_ids=[_OBS_ID_1],
        algorithm_version=_ALGORITHM_VERSION,
        value=Decimal("100"),
        unit=_UNIT,
        quality=_QUALITY,
    )
    v1_id = statistics_identity.metric_result_id(calc, _KNOWN_AT)

    # Resolution on UUID objects
    assert resolve_metric_identity_version(v2_id) == MetricIdentityVersion.V2
    assert resolve_metric_identity_version(v2_id) == "v2"
    assert resolve_metric_identity_version(v1_id) == MetricIdentityVersion.V1
    assert resolve_metric_identity_version(v1_id) == "v1"

    # Resolution on hex/hyphenated string representations
    assert resolve_metric_identity_version(str(v2_id)) == MetricIdentityVersion.V2
    assert resolve_metric_identity_version(str(v1_id)) == MetricIdentityVersion.V1

    # Canonical alias
    assert resolve_identity_version(v2_id) == MetricIdentityVersion.V2
    assert resolve_identity_version(v1_id) == MetricIdentityVersion.V1

    # Unknown / unsupported UUID version raises ValueError
    random_v4 = uuid4()
    with pytest.raises(ValueError, match="does not belong to metric identity v1 or v2"):
        resolve_metric_identity_version(random_v4)

    # Invalid type raises TypeError
    with pytest.raises(TypeError, match="Expected UUID or str"):
        resolve_metric_identity_version(12345)  # type: ignore[arg-type]


def test_semantic_versus_execution_parameters_is_an_explicit_tested_rule() -> None:
    """A8: Explicit, tested separation of semantic vs execution parameters."""
    # Verify execution parameter keys explicitly
    for key in (
        "known_at",
        "computed_at",
        "job_id",
        "attempt_id",
        "run_id",
        "worker_id",
        "execution_id",
        "batch_id",
        "trace_id",
        "requested_cut",
        "clock",
    ):
        assert key in EXECUTION_PARAMETER_KEYS
        assert is_execution_parameter(key) is True

    # Semantic parameters are preserved
    for semantic_key in ("window", "formula", "alpha", "period", "source_ids"):
        assert semantic_key not in EXECUTION_PARAMETER_KEYS
        assert is_execution_parameter(semantic_key) is False

    mixed_parameters = {
        "window": 24,
        "formula": "sum",
        "known_at": _KNOWN_AT.isoformat(),
        "job_id": "job-123",
        "batch_id": "batch-1",
    }
    filtered = filter_semantic_parameters(mixed_parameters)
    assert filtered == {"formula": "sum", "window": 24}

    # Injected known_at in parameters does not change v2 identity
    coords = _sample_coordinates()
    id_clean = metric_result_id_v2(**{**coords, "parameters": {"window": 24}})
    id_with_injected_known_at = metric_result_id_v2(
        **{**coords, "parameters": {"window": 24, "known_at": "2026-09-16T21:00:00Z"}}
    )
    assert id_clean == id_with_injected_known_at

    # Changed semantic parameter changes v2 identity
    id_diff_param = metric_result_id_v2(**{**coords, "parameters": {"window": 168}})
    assert id_clean != id_diff_param


def test_rule_is_pure_and_creates_no_schema_table_or_migration() -> None:
    """N3: Rule is pure: no I/O, no database connection, no tables or migrations."""
    coords = _sample_coordinates()
    result_uuid = metric_result_id_v2(**coords)
    assert isinstance(result_uuid, UUID)

    # Check migration file is intact
    migration_file = Path("src/investment_analyst/storage/migrations/001_initial.sql")
    assert migration_file.is_file()
    content = migration_file.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS metric_results" in content
    # No v2 migration table added
    assert "metric_results_v2" not in content


def test_decimal_and_utc_are_preserved_in_the_preimage() -> None:
    """N4: Exact Decimal and timezone-aware UTC timestamps are preserved."""
    # 1. Decimal exactness: trailing zeros and precision are not lost through float conversion
    coords = _sample_coordinates()
    preimage_exact = canonical_preimage_dict_v2(
        **{**coords, "parameters": {"threshold": Decimal("100.5000")}}
    )
    preimage_diff = canonical_preimage_dict_v2(
        **{**coords, "parameters": {"threshold": Decimal("100.5")}}
    )
    assert preimage_exact["parameters"]["threshold"] == Decimal("100.5000")
    assert preimage_diff["parameters"]["threshold"] == Decimal("100.5")
    assert (
        preimage_exact["parameters"]["threshold"].as_tuple()
        != preimage_diff["parameters"]["threshold"].as_tuple()
    )
    assert str(preimage_exact["parameters"]["threshold"]) != str(
        preimage_diff["parameters"]["threshold"]
    )

    json_exact = canonical_preimage_v2(
        **{**coords, "parameters": {"threshold": Decimal("100.5000")}}
    )
    json_diff = canonical_preimage_v2(**{**coords, "parameters": {"threshold": Decimal("100.5")}})
    assert json.loads(json_exact)["parameters"]["threshold"] == "100.5000"
    assert json.loads(json_diff)["parameters"]["threshold"] == "100.5"
    assert json_exact != json_diff

    # 2. UTC normalization: different offsets of the same UTC point-in-time produce identical output
    dt_utc = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
    dt_minus_5 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone(timedelta(hours=-5)))

    coords_utc = {**coords, "as_of": dt_utc, "available_at": dt_utc}
    coords_offset = {**coords, "as_of": dt_minus_5, "available_at": dt_minus_5}
    assert canonical_preimage_v2(**coords_utc) == canonical_preimage_v2(**coords_offset)
    assert metric_result_id_v2(**coords_utc) == metric_result_id_v2(**coords_offset)

    # 3. Naive datetime raises ValueError
    naive_dt = datetime(2026, 9, 16, 15, 0)
    with pytest.raises(ValueError, match="must include timezone information"):
        metric_result_id_v2(**{**coords, "as_of": naive_dt})


def test_no_production_caller_invokes_the_v2_rule() -> None:
    """X4: Zero production callers invoke metric_identity_v2 in this Work Block."""
    src_dir = Path("src/investment_analyst")
    violating_files: list[str] = []

    for path in src_dir.rglob("*.py"):
        if path.name == "metric_identity_v2.py":
            continue
        content = path.read_text(encoding="utf-8")
        if "metric_identity_v2" in content or "metric_result_id_v2" in content:
            violating_files.append(str(path))

    assert violating_files == [], (
        f"Production files unexpectedly reference metric_identity_v2: {violating_files}"
    )
