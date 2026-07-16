"""Unit tests for strict Apple workspace bootstrap contracts."""

import json
from datetime import UTC, date, datetime
from typing import Any, get_args, get_type_hints
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticSection,
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
    ConsolidatedTemporalContext,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplBootstrapStage,
    AaplBootstrapStageDetails,
    AaplBootstrapStageStatus,
    AaplBootstrapStageSummary,
    AaplMarketDateInterval,
    AaplMarketRefreshMode,
    AaplMarketRefreshPlan,
    AaplRefreshMode,
    AaplWorkspaceBootstrapRequest,
    AaplWorkspaceBootstrapSummary,
)
from investment_analyst.core.models import DataFrequency, DiagnosticMode

KNOWN_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)


def _request(**updates: object) -> AaplWorkspaceBootstrapRequest:
    values: dict[str, object] = {
        "market_start": date(2026, 1, 1),
        "market_end": date(2026, 1, 26),
        "fundamental_frequency": DataFrequency.QUARTERLY,
        "refresh_mode": AaplRefreshMode.AUTO,
        "requested_known_at": KNOWN_AT,
        "require_complete": False,
    }
    values.update(updates)
    return AaplWorkspaceBootstrapRequest(**values)


def _plan() -> AaplMarketRefreshPlan:
    return AaplMarketRefreshPlan(
        requested_start=date(2026, 1, 1),
        requested_end=date(2026, 1, 26),
        persisted_earliest=None,
        persisted_latest=None,
        fetch_intervals=(AaplMarketDateInterval(start=date(2026, 1, 1), end=date(2026, 1, 26)),),
        mode=AaplMarketRefreshMode.INITIAL,
        market_fetch_required=True,
        reason="No persisted Apple IEX daily bars were found.",
        traceability_verified=True,
    )


def _view(request: AaplWorkspaceBootstrapRequest) -> ConsolidatedDiagnosticView:
    query = ConsolidatedDiagnosticRequest(
        known_at=request.requested_known_at or KNOWN_AT,
        fundamental_frequency=request.fundamental_frequency,
    )
    return ConsolidatedDiagnosticView(
        request=query,
        status=ConsolidatedDiagnosticStatus.UNAVAILABLE,
        market=ConsolidatedDiagnosticSection(
            status=ConsolidatedSectionStatus.NOT_FOUND,
            mode=DiagnosticMode.MARKET,
            not_found_reason="market diagnostic not found",
        ),
        fundamental=ConsolidatedDiagnosticSection(
            status=ConsolidatedSectionStatus.NOT_FOUND,
            mode=DiagnosticMode.FUNDAMENTAL,
            not_found_reason="fundamental diagnostic not found",
        ),
        temporal_context=ConsolidatedTemporalContext(),
        diagnostics_examined=0,
        metric_results_examined=0,
        ignored_algorithm_versions=0,
        traceability_verified=True,
    )


def _stages() -> tuple[AaplBootstrapStageSummary, ...]:
    return tuple(
        AaplBootstrapStageSummary(
            stage=stage,
            status=AaplBootstrapStageStatus.COMPLETED,
            generated=0,
            created=0,
            reused=0,
            started_at=KNOWN_AT,
            completed_at=KNOWN_AT,
            details=AaplBootstrapStageDetails(effective_known_at=KNOWN_AT),
            traceability_verified=True,
        )
        for stage in AaplBootstrapStage
    )


def test_request_normalizes_offset_and_rejects_invalid_values() -> None:
    offset = datetime.fromisoformat("2026-07-14T07:00:00-05:00")
    request = _request(requested_known_at=offset)
    assert request.requested_known_at == KNOWN_AT

    with pytest.raises(ValidationError):
        _request(requested_known_at=datetime(2026, 7, 14, 12))
    with pytest.raises(ValidationError):
        _request(market_start=date(2026, 1, 26), market_end=date(2026, 1, 26))
    with pytest.raises(ValidationError):
        _request(fundamental_frequency=DataFrequency.DAY_1)
    with pytest.raises(ValidationError):
        _request(require_complete=1)
    with pytest.raises(ValidationError):
        AaplWorkspaceBootstrapRequest(
            market_start=date(2026, 1, 1),
            market_end=date(2026, 1, 26),
            fundamental_frequency=DataFrequency.QUARTERLY,
            unexpected=True,
        )


def test_stage_and_summary_are_strict_compact_and_non_combined() -> None:
    request = _request()
    view = _view(request)
    summary = AaplWorkspaceBootstrapSummary(
        workspace_id=uuid4(),
        source="SEC EDGAR and Alpaca Market Data",
        feed="iex",
        request=request,
        refresh_plan=_plan(),
        requested_known_at=KNOWN_AT,
        effective_known_at=KNOWN_AT,
        stages=_stages(),
        consolidated=view,
        overall_status=ConsolidatedDiagnosticStatus.UNAVAILABLE,
        raw_records_created=0,
        raw_records_reused=0,
        observations_created=0,
        observations_reused=0,
        metric_results_created=0,
        metric_results_reused=0,
        diagnostics_created=0,
        diagnostics_reused=0,
        traceability_verified=True,
    )
    payload = summary.to_json_dict()
    encoded = json.dumps(payload, sort_keys=True)
    assert payload["feed"] == "iex"
    assert payload["effective_known_at"] == KNOWN_AT.isoformat()
    assert payload["request"]["refresh_mode"] == "auto"
    assert payload["refresh_plan"]["mode"] == "initial"
    assert encoded.count('"consolidated"') == 1
    assert "combined_score" not in encoded
    assert "combined_verdict" not in encoded

    with pytest.raises(ValidationError):
        AaplBootstrapStageSummary(
            stage=AaplBootstrapStage.SEC_FETCH,
            status=AaplBootstrapStageStatus.COMPLETED,
            generated=True,
            created=0,
            reused=0,
            started_at=KNOWN_AT,
            completed_at=KNOWN_AT,
            details=AaplBootstrapStageDetails(),
            traceability_verified=True,
        )


def _contains_any(annotation: object) -> bool:
    return annotation is Any or any(_contains_any(item) for item in get_args(annotation))


def test_refresh_models_are_immutable_strict_and_do_not_use_any() -> None:
    plan = _plan()
    with pytest.raises(ValidationError):
        AaplMarketDateInterval(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=date(2026, 1, 2),
        )
    with pytest.raises(ValidationError):
        _request(refresh_mode="full")
    with pytest.raises(ValidationError):
        AaplMarketRefreshPlan(**{**plan.model_dump(), "market_fetch_required": False})
    with pytest.raises(ValidationError):
        AaplMarketRefreshPlan(**{**plan.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        plan.mode = AaplMarketRefreshMode.FULL

    for model in (AaplMarketDateInterval, AaplMarketRefreshPlan, AaplWorkspaceBootstrapRequest):
        assert not any(_contains_any(item) for item in get_type_hints(model).values())
