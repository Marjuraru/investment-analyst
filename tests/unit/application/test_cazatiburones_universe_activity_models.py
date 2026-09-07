"""Contract tests for the Cazatiburones universe activity index."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.application.cazatiburones_universe_activity_models import (
    CazatiburonesUniverseActivityFamily,
    CazatiburonesUniverseActivityRequest,
)
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
)


def test_contract_declares_cazatiburones_universe_activity_v1_and_sorted_unique_assets() -> None:
    request = CazatiburonesUniverseActivityRequest(
        known_at=datetime(2026, 7, 16, 15, 47, tzinfo=UTC),
        asset_ids=("equity:us:aapl", "equity:us:msft"),
    )

    assert request.asset_ids == ("equity:us:aapl", "equity:us:msft")

    with pytest.raises(ValidationError):
        CazatiburonesUniverseActivityRequest(
            known_at=request.known_at,
            asset_ids=("equity:us:msft", "equity:us:aapl"),
        )
    with pytest.raises(ValidationError):
        CazatiburonesUniverseActivityRequest(
            known_at=request.known_at,
            asset_ids=("equity:us:aapl", "equity:us:aapl"),
        )
    with pytest.raises(ValidationError):
        CazatiburonesUniverseActivityRequest(
            known_at=request.known_at,
            asset_ids=tuple(f"equity:us:{index:02d}" for index in range(41)),
        )


def test_family_validators_reject_present_without_evidence_and_missing_with_reason() -> None:
    with pytest.raises(ValidationError):
        CazatiburonesUniverseActivityFamily(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.PRESENT,
            statements=1,
        )
    with pytest.raises(ValidationError):
        CazatiburonesUniverseActivityFamily(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.MISSING,
            statements=0,
            not_evaluable_reason="malformed_persisted_record",
        )

    degraded = CazatiburonesUniverseActivityFamily(
        capability=CoverageCapability.SUPPORTED,
        evidence=EvidenceState.NOT_QUERIED,
        statements=0,
        not_evaluable_reason="malformed_persisted_record",
    )
    assert degraded.evidence is EvidenceState.NOT_QUERIED


def test_response_contains_no_cross_family_or_cross_asset_aggregate() -> None:
    family = CazatiburonesUniverseActivityFamily(
        capability=CoverageCapability.NOT_CONFIGURED,
        evidence=EvidenceState.NOT_QUERIED,
        statements=0,
    )
    assert "total" not in family.model_dump()
    assert "score" not in family.model_dump()
