"""Tests for strict consolidated diagnostic query models."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
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
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
)


def _diagnostic(mode: DiagnosticMode) -> DiagnosticResult:
    metric_id = uuid4()
    return DiagnosticResult(
        asset_id="equity:us:aapl",
        mode=mode,
        verdict=DiagnosticVerdict.NEUTRAL,
        final_score=Decimal("50"),
        confidence=Decimal("0.5"),
        as_of=datetime(2026, 6, 30, tzinfo=UTC),
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 2, tzinfo=UTC),
        components=[
            DiagnosticComponent(
                component_key="test",
                score=Decimal("50"),
                weight=Decimal("1"),
                weighted_contribution=Decimal("50"),
                metric_result_ids=[metric_id],
                explanation="Test component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric_id,
                direction=EvidenceDirection.NEUTRAL,
                contribution=Decimal("0"),
                reason="Test evidence.",
            )
        ],
        algorithm_version="test-v1",
        summary="Descriptive test diagnostic.",
        quality=DataQuality.VALID,
    )


def test_request_normalizes_offset_and_validates_scope() -> None:
    request = ConsolidatedDiagnosticRequest(
        known_at=datetime(2026, 7, 14, 1, tzinfo=timezone(timedelta(hours=-5))),
        fundamental_frequency=DataFrequency.QUARTERLY,
    )
    assert request.known_at == datetime(2026, 7, 14, 6, tzinfo=UTC)

    with pytest.raises(ValidationError):
        ConsolidatedDiagnosticRequest(
            asset_id="crypto:btc-usd",
            known_at=datetime(2026, 7, 14, tzinfo=UTC),
            fundamental_frequency=DataFrequency.QUARTERLY,
        )
    with pytest.raises(ValidationError):
        ConsolidatedDiagnosticRequest(
            known_at=datetime(2026, 7, 14),
            fundamental_frequency=DataFrequency.QUARTERLY,
        )
    with pytest.raises(ValidationError):
        ConsolidatedDiagnosticRequest(
            known_at=datetime(2026, 7, 14, tzinfo=UTC),
            fundamental_frequency=DataFrequency.QUARTERLY,
            market_as_of=date(2026, 7, 15),
        )


def test_section_status_contract_and_duplicate_ids() -> None:
    diagnostic = _diagnostic(DiagnosticMode.MARKET)
    identifier = diagnostic.components[0].metric_result_ids[0]
    section = ConsolidatedDiagnosticSection(
        status=ConsolidatedSectionStatus.AVAILABLE,
        mode=DiagnosticMode.MARKET,
        diagnostic=diagnostic,
        selected_metric_result_ids=(identifier,),
    )
    assert section.diagnostic == diagnostic

    with pytest.raises(ValidationError):
        ConsolidatedDiagnosticSection(
            status=ConsolidatedSectionStatus.AVAILABLE,
            mode=DiagnosticMode.MARKET,
            diagnostic=diagnostic,
            not_found_reason="unexpected",
            selected_metric_result_ids=(identifier,),
        )
    with pytest.raises(ValidationError):
        ConsolidatedDiagnosticSection(
            status=ConsolidatedSectionStatus.NOT_FOUND,
            mode=DiagnosticMode.MARKET,
        )
    with pytest.raises(ValidationError):
        ConsolidatedDiagnosticSection(
            status=ConsolidatedSectionStatus.AVAILABLE,
            mode=DiagnosticMode.MARKET,
            diagnostic=diagnostic,
            selected_metric_result_ids=(identifier, identifier),
        )


def test_view_status_gap_and_json_are_deterministic() -> None:
    request = ConsolidatedDiagnosticRequest(
        known_at=datetime(2026, 7, 14, tzinfo=UTC),
        fundamental_frequency=DataFrequency.QUARTERLY,
    )
    market_diagnostic = _diagnostic(DiagnosticMode.MARKET)
    fundamental_diagnostic = _diagnostic(DiagnosticMode.FUNDAMENTAL).model_copy(
        update={"as_of": datetime(2026, 3, 31, tzinfo=UTC)}
    )
    market_id = market_diagnostic.components[0].metric_result_ids[0]
    fundamental_id = fundamental_diagnostic.components[0].metric_result_ids[0]
    market = ConsolidatedDiagnosticSection(
        status=ConsolidatedSectionStatus.AVAILABLE,
        mode=DiagnosticMode.MARKET,
        diagnostic=market_diagnostic,
        selected_metric_result_ids=(market_id,),
    )
    fundamental = ConsolidatedDiagnosticSection(
        status=ConsolidatedSectionStatus.AVAILABLE,
        mode=DiagnosticMode.FUNDAMENTAL,
        diagnostic=fundamental_diagnostic,
        selected_metric_result_ids=(fundamental_id,),
    )
    gap = abs((market_diagnostic.as_of.date() - fundamental_diagnostic.as_of.date()).days)
    view = ConsolidatedDiagnosticView(
        request=request,
        status=ConsolidatedDiagnosticStatus.COMPLETE,
        market=market,
        fundamental=fundamental,
        temporal_context=ConsolidatedTemporalContext(
            market_as_of=market_diagnostic.as_of,
            fundamental_as_of=fundamental_diagnostic.as_of,
            reference_gap_days=gap,
            market_available_at=market_diagnostic.available_at,
            fundamental_available_at=fundamental_diagnostic.available_at,
        ),
        diagnostics_examined=2,
        metric_results_examined=2,
        ignored_algorithm_versions=0,
        traceability_verified=True,
    )
    payload = view.to_json_dict()
    assert payload["status"] == "complete"
    assert payload["temporal_context"]["reference_gap_days"] == gap
    assert payload["market"]["diagnostic"]["final_score"] == "50"
