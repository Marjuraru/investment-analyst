"""Tests for bounded point-in-time analytical rule replay."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.alerts.analytical_backtest import (
    AnalyticalBacktestAmbiguousSourceError,
    AnalyticalBacktestRequest,
    AnalyticalBacktestService,
    AnalyticalBacktestUnavailableError,
)
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_ANALYTICAL_RULES
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleRegistryStore,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_MARKET_RULE_ID = "market.activity.relative-volume-review"
_FUNDAMENTAL_RULE_ID = "fundamentals.quarterly-balance-growth-review"
_FUNDAMENTAL_SOURCE_ID = "sec-edgar:aapl:companyfacts"


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
            "frequency": "quarterly",
        },
        input_observation_ids=[UUID(f"63000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=algorithms[metric_key],
        quality=DataQuality.VALID,
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
