"""Tests for bounded point-in-time analytical rule replay."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.alerts.analytical_backtest import (
    AnalyticalBacktestAmbiguousSourceError,
    AnalyticalBacktestError,
    AnalyticalBacktestRequest,
    AnalyticalBacktestService,
    AnalyticalBacktestUnavailableError,
)
from investment_analyst.alerts.analytical_engine import AmbiguousAnalyticalMetricError
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_ANALYTICAL_RULES
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleRegistryStore,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.storage.repositories import DuckDBMetricResultRepository
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_MARKET_RULE_ID = "market.activity.relative-volume-review"
_FUNDAMENTAL_RULE_ID = "fundamentals.quarterly-balance-growth-review"
_ANNUAL_FUNDAMENTAL_RULE_ID = "fundamentals.annual-balance-growth-review"
_FUNDAMENTAL_SOURCE_ID = "sec-edgar:aapl:companyfacts"
_V2_OBSERVATION_NAMESPACE = UUID("6b000000-0000-4000-8000-000000000001")
_RSI_KEY = "market.technical.rsi"
_MACD_HISTOGRAM_KEY = "market.technical.macd.histogram"
_RSI_LEGACY_ALGORITHM = "market-rsi-wilder-v1-decimal34"
_RSI_INCREMENTAL_ALGORITHM = "market-rsi-incremental-v2-decimal34"
_MACD_INCREMENTAL_ALGORITHM = "market-macd-incremental-v2-decimal34"


def _market_metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    known_at: datetime,
    source_id: str = _SOURCE_ID,
) -> MetricResult:
    return MetricResult(
        result_id=UUID(f"60000000-0000-4000-8000-{identifier:012d}"),
        asset_id=_ASSET_ID,
        metric_key="market.history.relative_volume",
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters={
            "source_id": source_id,
            "known_at": known_at.isoformat(),
            "window": 20,
        },
        input_observation_ids=[UUID(f"61000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version="market-relative-volume-v1-decimal34",
        quality=DataQuality.PARTIAL,
    )


def _fundamental_metric(
    metric_key: str,
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    available_at: datetime,
    frequency: str = "quarterly",
) -> MetricResult:
    algorithms = {
        "fundamental.liabilities_to_assets": ("sec-fundamental-liabilities-to-assets-v1-decimal34"),
        "fundamental.net_margin": "sec-fundamental-net-margin-v1-decimal34",
        "fundamental.revenue_yoy_growth": ("sec-fundamental-revenue-yoy-growth-v1-decimal34"),
    }
    return MetricResult(
        result_id=UUID(f"62000000-0000-4000-8000-{identifier:012d}"),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters={
            "source_id": _FUNDAMENTAL_SOURCE_ID,
            "frequency": frequency,
        },
        input_observation_ids=[UUID(f"63000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=algorithms[metric_key],
        quality=DataQuality.VALID,
    )


def _v2_market_metric(
    value: str,
    *,
    as_of: datetime,
    available_at: datetime,
) -> MetricResult:
    """Build a v2 market row: semantic identity without the legacy known_at parameter."""
    input_observation_ids = [uuid5(_V2_OBSERVATION_NAMESPACE, as_of.isoformat())]
    parameters = {"source_id": _SOURCE_ID, "window": 20}
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=_ASSET_ID,
            metric_key="market.history.relative_volume",
            input_observation_ids=input_observation_ids,
            algorithm_version="market-relative-volume-v1-decimal34",
            as_of=as_of,
            available_at=available_at,
            unit="ratio",
            quality=DataQuality.PARTIAL,
            parameters=parameters,
        ),
        asset_id=_ASSET_ID,
        metric_key="market.history.relative_volume",
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters=parameters,
        input_observation_ids=input_observation_ids,
        algorithm_version="market-relative-volume-v1-decimal34",
        quality=DataQuality.PARTIAL,
    )


def _v2_technical_metric(
    value: str,
    *,
    metric_key: str,
    algorithm_version: str,
    as_of: datetime,
    available_at: datetime,
    unit: str,
    parameters: dict[str, object],
) -> MetricResult:
    """Build one v2 technical row with semantic identity and no legacy cut parameter."""
    input_observation_ids = [
        uuid5(
            _V2_OBSERVATION_NAMESPACE,
            f"{metric_key}:{algorithm_version}:{as_of.isoformat()}:{value}",
        )
    ]
    scoped_parameters = {"source_id": _SOURCE_ID, **parameters}
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=_ASSET_ID,
            metric_key=metric_key,
            input_observation_ids=input_observation_ids,
            algorithm_version=algorithm_version,
            as_of=as_of,
            available_at=available_at,
            unit=unit,
            quality=DataQuality.PARTIAL,
            parameters=scoped_parameters,
        ),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters=scoped_parameters,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        quality=DataQuality.PARTIAL,
    )


def _v1_technical_metric(
    value: str,
    *,
    metric_key: str,
    algorithm_version: str,
    as_of: datetime,
    known_at: datetime,
    unit: str,
    parameters: dict[str, object],
) -> MetricResult:
    """Build one legacy technical row with its exact persisted cut parameter."""
    scoped_parameters = {
        "source_id": _SOURCE_ID,
        "known_at": known_at.isoformat(),
        **parameters,
    }
    return MetricResult(
        result_id=uuid5(
            NAMESPACE_DNS,
            f"v1-{metric_key}-{algorithm_version}-{as_of.isoformat()}-{value}",
        ),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters=scoped_parameters,
        input_observation_ids=[uuid5(_V2_OBSERVATION_NAMESPACE, f"input:{as_of.isoformat()}")],
        algorithm_version=algorithm_version,
        quality=DataQuality.PARTIAL,
    )


def _service(
    tmp_path: Path,
    metrics: tuple[MetricResult, ...],
) -> AnalyticalBacktestService:
    workspace_service = WorkspaceService(environ={}, home=tmp_path / "home")
    paths = workspace_service.initialize(tmp_path / "workspace").paths
    storage = workspace_service.open_storage(paths, WorkspaceAccessMode.READ_WRITE)
    try:
        for metric in metrics:
            storage.metric_results.save(metric)
    finally:
        storage.close()
    runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
    registry = AnalyticalRuleRegistryStore(
        paths.state_root / "rules.json",
        INITIAL_ANALYTICAL_RULES,
    )
    return AnalyticalBacktestService(runtime, paths.root, registry)


def test_market_backtest_simulates_confirmations_hysteresis_cooldown_and_replay(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    values = ("1.6", "1.7", "1.3", "1.1", "1.8", "1.9")
    metrics = tuple(
        _market_metric(
            value,
            identifier=index,
            as_of=start + timedelta(days=index - 1),
            known_at=start + timedelta(days=index - 1, hours=12),
        )
        for index, value in enumerate(values, start=1)
    )
    service = _service(tmp_path, metrics)
    request = AnalyticalBacktestRequest(
        rule_id=_MARKET_RULE_ID,
        asset_id=_ASSET_ID,
        max_cuts=20,
    )

    first = service.run(request)
    repeated = service.run(request)

    assert first == repeated
    assert first.total_available_cuts == 6
    assert first.matched_count == 4
    assert first.not_evaluable_count == 0
    assert first.candidate_activation_count == 2
    assert first.candidate_resolution_count == 1
    assert first.match_rate == Decimal("0.6667")
    assert tuple(item.candidate_opened for item in first.evaluations) == (
        False,
        True,
        False,
        False,
        False,
        True,
    )
    assert first.evaluations[2].result.retained is True
    assert first.evaluations[3].result.retained is False


def test_backtest_bounds_query_to_rule_condition_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    metrics = (
        _market_metric(
            "1.6",
            identifier=31,
            as_of=start,
            known_at=start + timedelta(hours=12),
        ),
        _fundamental_metric(
            "fundamental.net_margin",
            "0.2",
            identifier=32,
            as_of=start,
            available_at=start + timedelta(hours=12),
        ),
    )
    seen_metric_keys: list[tuple[str, ...] | None] = []
    original_list = DuckDBMetricResultRepository.list

    def tracked_list(
        repository: DuckDBMetricResultRepository,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]:
        seen_metric_keys.append(metric_keys)
        return original_list(
            repository,
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=metric_keys,
            as_of_from=as_of_from,
            as_of_to=as_of_to,
        )

    monkeypatch.setattr(DuckDBMetricResultRepository, "list", tracked_list)
    result = _service(tmp_path, metrics).run(
        AnalyticalBacktestRequest(
            rule_id=_MARKET_RULE_ID,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert result.total_available_cuts == 1
    assert seen_metric_keys == [("market.history.relative_volume",)]


def test_backtest_bounded_query_does_not_hide_ambiguous_metric_revision(
    tmp_path: Path,
) -> None:
    known_at = datetime(2026, 7, 20, 12, tzinfo=UTC)
    metrics = (
        _market_metric(
            "1.6",
            identifier=41,
            as_of=known_at - timedelta(days=1),
            known_at=known_at,
        ),
        _market_metric(
            "1.7",
            identifier=42,
            as_of=known_at - timedelta(days=1),
            known_at=known_at,
        ),
    )

    with pytest.raises(AmbiguousAnalyticalMetricError, match="multiple compatible"):
        _service(tmp_path, metrics).run(
            AnalyticalBacktestRequest(
                rule_id=_MARKET_RULE_ID,
                asset_id=_ASSET_ID,
                max_cuts=20,
            )
        )


def test_backtest_is_bounded_to_latest_cuts_and_reports_truncation(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = tuple(
        _market_metric(
            "1.0",
            identifier=index,
            as_of=start + timedelta(days=index),
            known_at=start + timedelta(days=index, hours=12),
        )
        for index in range(1, 26)
    )

    result = _service(tmp_path, metrics).run(
        AnalyticalBacktestRequest(
            rule_id=_MARKET_RULE_ID,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert result.total_available_cuts == 25
    assert len(result.evaluations) == 20
    assert result.truncated is True
    assert result.evaluations[0].result.known_at == start + timedelta(
        days=6,
        hours=12,
    )


def test_fundamental_backtest_uses_available_at_and_latest_revision(tmp_path: Path) -> None:
    as_of = datetime(2026, 6, 30, tzinfo=UTC)
    first_available = datetime(2026, 7, 20, 12, tzinfo=UTC)
    revised_available = first_available + timedelta(days=1)
    metrics = (
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.7",
            identifier=1,
            as_of=as_of,
            available_at=first_available,
        ),
        _fundamental_metric(
            "fundamental.net_margin",
            "0.2",
            identifier=2,
            as_of=as_of,
            available_at=first_available,
        ),
        _fundamental_metric(
            "fundamental.revenue_yoy_growth",
            "0.1",
            identifier=3,
            as_of=as_of,
            available_at=first_available,
        ),
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.5",
            identifier=4,
            as_of=as_of,
            available_at=revised_available,
        ),
    )

    result = _service(tmp_path, metrics).run(
        AnalyticalBacktestRequest(
            rule_id=_FUNDAMENTAL_RULE_ID,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert len(result.evaluations) == 2
    assert result.evaluations[0].result.matched is False
    assert result.evaluations[1].result.matched is True
    assert result.evaluations[1].result.conditions[0].observed_value == Decimal("0.5")
    assert result.candidate_activation_count == 1


def test_annual_fundamental_backtest_replays_only_annual_evidence(tmp_path: Path) -> None:
    first_as_of = datetime(2025, 12, 31, tzinfo=UTC)
    second_as_of = datetime(2026, 6, 30, tzinfo=UTC)
    metrics = (
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.7",
            identifier=1,
            as_of=first_as_of,
            available_at=datetime(2026, 2, 15, tzinfo=UTC),
            frequency="annual",
        ),
        _fundamental_metric(
            "fundamental.net_margin",
            "0.2",
            identifier=2,
            as_of=first_as_of,
            available_at=datetime(2026, 2, 15, tzinfo=UTC),
            frequency="annual",
        ),
        _fundamental_metric(
            "fundamental.revenue_yoy_growth",
            "0.1",
            identifier=3,
            as_of=first_as_of,
            available_at=datetime(2026, 2, 15, tzinfo=UTC),
            frequency="annual",
        ),
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.5",
            identifier=4,
            as_of=second_as_of,
            available_at=datetime(2026, 8, 15, tzinfo=UTC),
            frequency="annual",
        ),
        _fundamental_metric(
            "fundamental.net_margin",
            "0.25",
            identifier=5,
            as_of=second_as_of,
            available_at=datetime(2026, 8, 15, tzinfo=UTC),
            frequency="annual",
        ),
        _fundamental_metric(
            "fundamental.revenue_yoy_growth",
            "0.12",
            identifier=6,
            as_of=second_as_of,
            available_at=datetime(2026, 8, 15, tzinfo=UTC),
            frequency="annual",
        ),
    )

    result = _service(tmp_path, metrics).run(
        AnalyticalBacktestRequest(
            rule_id=_ANNUAL_FUNDAMENTAL_RULE_ID,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert result.total_available_cuts == 2
    assert result.evaluations[0].result.matched is False
    assert result.evaluations[1].result.matched is True
    assert result.evaluations[1].result.conditions[0].observed_value == Decimal("0.5")
    assert result.matched_count == 1


def test_backtest_rejects_missing_or_ambiguous_sources(tmp_path: Path) -> None:
    empty = _service(tmp_path / "empty", ())
    with pytest.raises(AnalyticalBacktestUnavailableError, match="no compatible"):
        empty.run(
            AnalyticalBacktestRequest(
                rule_id=_MARKET_RULE_ID,
                asset_id=_ASSET_ID,
            )
        )

    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    ambiguous = _service(
        tmp_path / "ambiguous",
        (
            _market_metric(
                "1.5",
                identifier=1,
                as_of=known_at - timedelta(days=1),
                known_at=known_at,
            ),
            _market_metric(
                "1.6",
                identifier=2,
                as_of=known_at,
                known_at=known_at + timedelta(days=1),
                source_id="other-source",
            ),
        ),
    )
    with pytest.raises(AnalyticalBacktestAmbiguousSourceError, match="multiple sources"):
        ambiguous.run(
            AnalyticalBacktestRequest(
                rule_id=_MARKET_RULE_ID,
                asset_id=_ASSET_ID,
            )
        )


def test_backtest_request_rejects_boolean_or_unbounded_limits() -> None:
    base = {
        "rule_id": _MARKET_RULE_ID,
        "asset_id": _ASSET_ID,
    }
    for value in (True, 19, 501):
        with pytest.raises(ValidationError):
            AnalyticalBacktestRequest.model_validate({**base, "max_cuts": value})


def test_market_cuts_are_derived_from_v2_rows_without_an_empty_cut_set(tmp_path: Path) -> None:
    """A9: v2 market rows produce cuts from available_at and the replay emits results."""
    start = datetime(2026, 7, 20, tzinfo=UTC)
    values = ("1.6", "1.7", "1.3", "1.1")
    metrics = tuple(
        _v2_market_metric(
            value,
            as_of=start + timedelta(days=index),
            available_at=start + timedelta(days=index, hours=12),
        )
        for index, value in enumerate(values)
    )

    result = _service(tmp_path, metrics).run(
        AnalyticalBacktestRequest(
            rule_id=_MARKET_RULE_ID,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert result.total_available_cuts == 4
    assert len(result.evaluations) == 4
    assert result.first_known_at == start + timedelta(hours=12)
    assert result.last_known_at == start + timedelta(days=3, hours=12)
    assert result.matched_count == 2
    assert result.evaluations[0].result.conditions[0].metric_result_id == metrics[0].result_id


@pytest.mark.parametrize(
    ("known_at_parameter", "message"),
    [
        ("not-a-timestamp", "market metric known_at parameter is invalid"),
        ("2026-07-20T00:00:00", "market metric known_at parameter must include timezone"),
    ],
)
def test_market_backtest_preserves_legacy_known_at_parameter_errors(
    tmp_path: Path,
    known_at_parameter: str,
    message: str,
) -> None:
    """A3: the legacy v1 cut errors survive the version-aware cut derivation."""
    start = datetime(2026, 7, 20, tzinfo=UTC)
    metric = _market_metric(
        "1.6",
        identifier=51,
        as_of=start,
        known_at=start + timedelta(hours=12),
    ).model_copy(
        update={
            "parameters": {
                "source_id": _SOURCE_ID,
                "known_at": known_at_parameter,
                "window": 20,
            }
        }
    )

    with pytest.raises(AnalyticalBacktestError, match=message):
        _service(tmp_path, (metric,)).run(
            AnalyticalBacktestRequest(
                rule_id=_MARKET_RULE_ID,
                asset_id=_ASSET_ID,
            )
        )


@pytest.mark.parametrize(
    ("rule_id", "metric_key", "algorithm_version", "unit", "parameters", "values"),
    (
        (
            "market.technical.rsi-incremental-low-review",
            _RSI_KEY,
            _RSI_INCREMENTAL_ALGORITHM,
            "index",
            {"window": 14},
            ("25", "24", "40", "22"),
        ),
        (
            "market.technical.macd-incremental-positive-histogram-review",
            _MACD_HISTOGRAM_KEY,
            _MACD_INCREMENTAL_ALGORITHM,
            "USD",
            {"fast_window": 12, "slow_window": 26, "signal_window": 9},
            ("0.5", "0.7", "-0.2", "0.9"),
        ),
    ),
)
def test_incremental_technical_rules_replay_v2_evidence(
    tmp_path: Path,
    rule_id: str,
    metric_key: str,
    algorithm_version: str,
    unit: str,
    parameters: dict[str, object],
    values: tuple[str, ...],
) -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    metrics = tuple(
        _v2_technical_metric(
            value,
            metric_key=metric_key,
            algorithm_version=algorithm_version,
            as_of=start + timedelta(days=index),
            available_at=start + timedelta(days=index, hours=12),
            unit=unit,
            parameters=parameters,
        )
        for index, value in enumerate(values)
    )

    result = _service(tmp_path, metrics).run(
        AnalyticalBacktestRequest(
            rule_id=rule_id,
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert result.total_available_cuts == 4
    assert len(result.evaluations) == 4
    assert result.matched_count == 3
    assert result.candidate_activation_count == 1
    assert result.first_known_at == start + timedelta(hours=12)
    assert result.last_known_at == start + timedelta(days=3, hours=12)
    assert result.evaluations[0].result.conditions[0].metric_result_id == metrics[0].result_id
    assert result.evaluations[1].candidate_opened is True
    assert {item.result.rule.rule_id for item in result.evaluations} == {rule_id}


def test_legacy_and_incremental_backtests_keep_versions_separate(tmp_path: Path) -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    legacy_metric = _v1_technical_metric(
        "25",
        metric_key=_RSI_KEY,
        algorithm_version=_RSI_LEGACY_ALGORITHM,
        as_of=start,
        known_at=start + timedelta(hours=12),
        unit="index",
        parameters={"window": 14},
    )
    incremental_metric = _v2_technical_metric(
        "24",
        metric_key=_RSI_KEY,
        algorithm_version=_RSI_INCREMENTAL_ALGORITHM,
        as_of=start + timedelta(days=1),
        available_at=start + timedelta(days=1, hours=12),
        unit="index",
        parameters={"window": 14},
    )
    service = _service(tmp_path, (legacy_metric, incremental_metric))

    legacy = service.run(
        AnalyticalBacktestRequest(
            rule_id="market.technical.rsi-low-review",
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )
    incremental = service.run(
        AnalyticalBacktestRequest(
            rule_id="market.technical.rsi-incremental-low-review",
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert legacy.total_available_cuts == 1
    assert legacy.evaluations[0].result.conditions[0].metric_result_id == legacy_metric.result_id
    assert incremental.total_available_cuts == 1
    assert (
        incremental.evaluations[0].result.conditions[0].metric_result_id
        == incremental_metric.result_id
    )
