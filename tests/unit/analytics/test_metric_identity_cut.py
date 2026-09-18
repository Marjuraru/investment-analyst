"""Unit tests for the version-aware point-in-time cut eligibility rule."""

from __future__ import annotations

import builtins
import io
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid5

import pytest

from investment_analyst.alerts.analytical_monitor import AnalyticalMetricSnapshotSelector
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_MARKET_ACTIVITY_RULE
from investment_analyst.analytics import metric_identity_cut, metric_identity_v2
from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import (
    MarketDiagnosticRequest,
    MarketMetricSnapshot,
)
from investment_analyst.analytics.market.diagnostic_selection import (
    InvalidMetricContextError,
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.metric_identity_cut import (
    CutIdentityVersion,
    LegacyKnownAtState,
    MetricCutContractError,
    MetricCutEligibility,
    metric_cut_eligibility,
    probe_legacy_known_at,
    resolve_cut_identity_version,
)
from investment_analyst.analytics.metric_identity_v2 import (
    MetricIdentityVersion,
    metric_result_id_v2,
    resolve_metric_identity_version,
)
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.storage import LocalStorage, StoragePaths

_NAMESPACE = UUID("5c1d4f6e-2b7a-4d43-9b0e-3f5a6c7d8e90")
_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_METRIC_KEY = "market.history.relative_volume"
_RETURN_KEY = "market.history.simple_return_1d"
_SMA_KEY = "market.history.sma"
_VOLATILITY_KEY = "market.history.rolling_daily_volatility"
_ALGORITHM_VERSION = "market-relative-volume-v1-decimal34"
_AS_OF = datetime(2026, 7, 10, tzinfo=UTC)
_AVAILABLE_AT = datetime(2026, 7, 10, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
_LATER_CUT = _KNOWN_AT + timedelta(days=1)
_OBSERVATION_ID = UUID("6a000000-0000-4000-8000-000000000001")


def _request() -> MarketDiagnosticRequest:
    return MarketDiagnosticRequest(
        query=HistoricalBarQuery(
            asset_id=_ASSET_ID,
            source_id=_SOURCE_ID,
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 15, tzinfo=UTC),
            known_at=_KNOWN_AT,
        ),
        short_sma_window=2,
        long_sma_window=3,
        volatility_window=2,
        relative_volume_window=2,
    )


def _build_metric(
    identifier: UUID,
    parameters: dict[str, object],
    *,
    available_at: datetime = _AVAILABLE_AT,
    metric_key: str = _METRIC_KEY,
    unit: str = "ratio",
) -> MetricResult:
    return MetricResult(
        result_id=identifier,
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal("1.5"),
        unit=unit,
        as_of=_AS_OF,
        available_at=available_at,
        computed_at=max(available_at, _KNOWN_AT),
        parameters=parameters,
        input_observation_ids=[_OBSERVATION_ID],
        algorithm_version=_ALGORITHM_VERSION,
        quality=DataQuality.VALID,
    )


def _default_legacy_parameters() -> dict[str, object]:
    return {"source_id": _SOURCE_ID, "known_at": _KNOWN_AT.isoformat()}


def _v1_metric(
    label: str,
    parameters: dict[str, object] | None = None,
    *,
    available_at: datetime = _AVAILABLE_AT,
    metric_key: str = _METRIC_KEY,
    unit: str = "ratio",
) -> MetricResult:
    resolved = _default_legacy_parameters() if parameters is None else dict(parameters)
    return _build_metric(
        uuid5(_NAMESPACE, label),
        resolved,
        available_at=available_at,
        metric_key=metric_key,
        unit=unit,
    )


def _v2_metric(
    parameters: dict[str, object] | None = None,
    *,
    available_at: datetime = _AVAILABLE_AT,
    metric_key: str = _METRIC_KEY,
) -> MetricResult:
    resolved: dict[str, object] = (
        {"source_id": _SOURCE_ID, "window": 20} if parameters is None else dict(parameters)
    )
    identifier = metric_result_id_v2(
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        input_observation_ids=[_OBSERVATION_ID],
        algorithm_version=_ALGORITHM_VERSION,
        as_of=_AS_OF,
        available_at=available_at,
        unit="ratio",
        quality=DataQuality.VALID,
        parameters=resolved,
    )
    return _build_metric(identifier, resolved, available_at=available_at, metric_key=metric_key)


def test_cut_eligibility_resolves_version_from_result_id_without_storage(tmp_path: Path) -> None:
    """A1: the rule returns eligibility and version for v1 and v2 rows without storage."""
    legacy = _v1_metric("a1-legacy")
    semantic = _v2_metric()
    absent_workspace = tmp_path / "no-workspace-exists"

    legacy_cut = metric_cut_eligibility(legacy, _KNOWN_AT)
    assert legacy_cut.version is CutIdentityVersion.V1
    assert legacy_cut.eligible is True
    assert legacy_cut.legacy is not None
    assert legacy_cut.legacy.value == _KNOWN_AT
    assert metric_cut_eligibility(legacy, _LATER_CUT).eligible is False

    semantic_cut = metric_cut_eligibility(semantic, _KNOWN_AT)
    assert semantic_cut.version is CutIdentityVersion.V2
    assert semantic_cut.eligible is True
    assert semantic_cut.legacy is None
    assert metric_cut_eligibility(semantic, _AVAILABLE_AT).eligible is True
    assert (
        metric_cut_eligibility(semantic, _AVAILABLE_AT - timedelta(microseconds=1)).eligible
        is False
    )
    assert not absent_workspace.exists()
    assert legacy.result_id.version == 5
    assert semantic.result_id.version == 8


@pytest.mark.parametrize("uuid_version", [1, 3, 4, 6, 7])
def test_unknown_uuid_version_degrades_to_legacy_rule_and_never_raises(uuid_version: int) -> None:
    """A2: any identifier that is not UUID5/UUID8 degrades to the legacy rule case by case."""
    identifier = UUID(f"00000000-0000-{uuid_version}000-8000-000000000001")
    degraded = _build_metric(identifier, {"source_id": _SOURCE_ID})

    assert identifier.version == uuid_version
    assert resolve_cut_identity_version(degraded) is CutIdentityVersion.V1
    absent = metric_cut_eligibility(degraded, _KNOWN_AT)
    assert absent.version is CutIdentityVersion.V1
    assert absent.eligible is False
    assert absent.legacy is not None
    assert absent.legacy.state is LegacyKnownAtState.ABSENT

    for parameters, expected_state in (
        ({"known_at": 12345}, LegacyKnownAtState.NOT_STRING),
        ({"known_at": "not-a-timestamp"}, LegacyKnownAtState.UNPARSEABLE),
        ({"known_at": "2026-07-20T00:00:00"}, LegacyKnownAtState.NAIVE),
        ({"known_at": _KNOWN_AT.isoformat()}, LegacyKnownAtState.VALUE),
    ):
        case = _build_metric(identifier, {"source_id": _SOURCE_ID, **parameters})
        eligibility = metric_cut_eligibility(case, _KNOWN_AT)
        assert eligibility.version is CutIdentityVersion.V1
        assert eligibility.legacy is not None
        assert eligibility.legacy.state is expected_state


@pytest.mark.parametrize(
    ("case", "parameters", "selector_message", "snapshot_message"),
    [
        ("missing", {}, "has an invalid known_at parameter", "must be an ISO-8601 string"),
        (
            "not-string",
            {"known_at": 12345},
            "has an invalid known_at parameter",
            "must be an ISO-8601 string",
        ),
        (
            "unparseable",
            {"known_at": "not-a-timestamp"},
            "has an invalid known_at parameter",
            "is not valid ISO-8601",
        ),
        (
            "naive",
            {"known_at": "2026-07-20T00:00:00"},
            "has a naive known_at parameter",
            "must include timezone information",
        ),
    ],
)
def test_v1_rule_requires_known_at_parameter_with_identical_errors(
    tmp_path: Path,
    case: str,
    parameters: dict[str, object],
    selector_message: str,
    snapshot_message: str,
) -> None:
    """A3: the v1 rule keeps the exact legacy error classes and messages in the read paths."""
    malformed = _v1_metric(f"a3-{case}", {"source_id": _SOURCE_ID, "window": 20, **parameters})
    eligibility = metric_cut_eligibility(malformed, _KNOWN_AT)

    assert eligibility.version is CutIdentityVersion.V1
    assert eligibility.eligible is False
    assert eligibility.legacy is not None
    assert eligibility.legacy.state is not LegacyKnownAtState.VALUE

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(malformed)
        with pytest.raises(
            InvalidMetricContextError,
            match=f"metric result {malformed.result_id} {selector_message}",
        ):
            MarketDiagnosticMetricSelector(storage).candidates(_request())

    with pytest.raises(ValueError, match=f"metric parameter 'known_at' {snapshot_message}"):
        MarketMetricSnapshot.model_validate(
            {
                "asset_id": _ASSET_ID,
                "source_id": _SOURCE_ID,
                "known_at": _KNOWN_AT,
                "as_of": _AS_OF,
                "simple_return": _v1_metric(
                    f"a3-{case}-return",
                    {"source_id": _SOURCE_ID, "known_at": _KNOWN_AT.isoformat(), "periods": 1},
                    metric_key=_RETURN_KEY,
                ),
                "short_sma": _v1_metric(
                    f"a3-{case}-short",
                    {"source_id": _SOURCE_ID, "window": 2, **parameters},
                    metric_key=_SMA_KEY,
                    unit="USD",
                ),
                "long_sma": _v1_metric(
                    f"a3-{case}-long",
                    {"source_id": _SOURCE_ID, "window": 3},
                    metric_key=_SMA_KEY,
                    unit="USD",
                ),
                "rolling_volatility": _v1_metric(
                    f"a3-{case}-volatility",
                    {"source_id": _SOURCE_ID, "window": 2},
                    metric_key=_VOLATILITY_KEY,
                ),
                "relative_volume": _v1_metric(
                    f"a3-{case}-volume",
                    {"source_id": _SOURCE_ID, "window": 2},
                ),
            }
        )


def test_v2_rule_uses_available_at_and_rejects_a_known_at_parameter() -> None:
    """A4: v2 is eligible by available_at and fails closed when it carries known_at."""
    semantic = _v2_metric()
    assert metric_cut_eligibility(semantic, _KNOWN_AT).eligible is True
    assert metric_cut_eligibility(semantic, _AVAILABLE_AT).eligible is True
    assert (
        metric_cut_eligibility(semantic, _AVAILABLE_AT - timedelta(microseconds=1)).eligible
        is False
    )

    violation = _v2_metric({"known_at": _KNOWN_AT.isoformat()})
    assert resolve_cut_identity_version(violation) is CutIdentityVersion.V2
    with pytest.raises(MetricCutContractError, match="legacy known_at parameter"):
        metric_cut_eligibility(violation, _KNOWN_AT)


def test_rule_is_pure_and_performs_no_storage_or_filesystem_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N4: the rule is pure: no storage, no filesystem, no ambient state."""
    source = Path(metric_identity_cut.__file__).read_text(encoding="utf-8")
    for forbidden_dependency in (
        "investment_analyst.storage",
        "investment_analyst.workspace",
        "investment_analyst.providers",
        "investment_analyst.application",
        "investment_analyst.alerts",
    ):
        assert f"from {forbidden_dependency}" not in source
        assert f"import {forbidden_dependency}" not in source
    imported_modules = {
        value.__name__
        for value in vars(metric_identity_cut).values()
        if isinstance(value, ModuleType)
    }
    assert imported_modules == set()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("cut rule touched the filesystem")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(io, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)

    legacy = _v1_metric("n4-legacy")
    semantic = _v2_metric()
    for metric, cut in (
        (legacy, _KNOWN_AT),
        (legacy, _LATER_CUT),
        (semantic, _KNOWN_AT),
        (semantic, _AVAILABLE_AT - timedelta(microseconds=1)),
    ):
        assert isinstance(metric_cut_eligibility(metric, cut), MetricCutEligibility)


def test_no_v2_identity_is_computed_or_persisted_by_any_read_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X3: reading v1 and v2 rows computes no v2 identity and persists nothing."""
    legacy = _v1_metric("x3-legacy")
    semantic = _v2_metric()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("read path computed a v2 identity")

    monkeypatch.setattr(metric_identity_v2, "metric_result_id_v2", forbidden)
    monkeypatch.setattr(metric_identity_v2, "metric_result_id_from_model_v2", forbidden)

    assert metric_cut_eligibility(legacy, _KNOWN_AT).version is CutIdentityVersion.V1
    assert metric_cut_eligibility(semantic, _KNOWN_AT).version is CutIdentityVersion.V2
    selected = AnalyticalMetricSnapshotSelector().select(
        rule=INITIAL_MARKET_ACTIVITY_RULE,
        metrics=(semantic,),
        source_id=_SOURCE_ID,
        known_at=_KNOWN_AT,
    )
    assert selected == (semantic,)
    assert selected[0].result_id == semantic.result_id


def test_version_is_never_inferred_from_parameters_metric_key_or_domain() -> None:
    """X6: the version is read from result_id only, never from parameters, key or domain."""
    v1_without_parameter = _v1_metric("x6-v1", {"source_id": _SOURCE_ID, "window": 20})
    assert resolve_cut_identity_version(v1_without_parameter) is CutIdentityVersion.V1
    degraded = metric_cut_eligibility(v1_without_parameter, _KNOWN_AT)
    assert degraded.version is CutIdentityVersion.V1
    assert degraded.eligible is False
    assert degraded.legacy is not None
    assert degraded.legacy.state is LegacyKnownAtState.ABSENT
    assert probe_legacy_known_at(v1_without_parameter).state is LegacyKnownAtState.ABSENT

    v2_with_parameter = _v2_metric({"known_at": _KNOWN_AT.isoformat()})
    assert resolve_cut_identity_version(v2_with_parameter) is CutIdentityVersion.V2
    with pytest.raises(MetricCutContractError, match="under metric identity v2"):
        metric_cut_eligibility(v2_with_parameter, _KNOWN_AT)

    assert (
        resolve_cut_identity_version(
            _v1_metric("x6-fundamental", metric_key="fundamental.net_margin")
        )
        is CutIdentityVersion.V1
    )
    assert (
        resolve_cut_identity_version(_v2_metric(metric_key="crypto.funding.sum_1h"))
        is CutIdentityVersion.V2
    )


@pytest.mark.parametrize("uuid_version", [1, 3, 4, 5, 6, 7, 8])
def test_cut_version_resolution_is_equivalent_to_the_audited_identity_adapter(
    uuid_version: int,
) -> None:
    """X2: the cut rule mirrors the audited v1/v2 vocabulary and adds totality to it."""
    identifier = UUID(f"00000000-0000-{uuid_version}000-8000-000000000001")
    metric = _build_metric(identifier, {"source_id": _SOURCE_ID})
    expected = "v2" if uuid_version == 8 else "v1"

    assert resolve_cut_identity_version(metric).value == expected
    if uuid_version in {5, 8}:
        audited = resolve_metric_identity_version(identifier)
        expected_version = (
            MetricIdentityVersion.V2 if uuid_version == 8 else MetricIdentityVersion.V1
        )
        assert audited is expected_version
        assert audited.value == expected
    else:
        with pytest.raises(ValueError, match="does not belong to metric identity v1 or v2"):
            resolve_metric_identity_version(identifier)
        assert metric_cut_eligibility(metric, _KNOWN_AT).version is CutIdentityVersion.V1
