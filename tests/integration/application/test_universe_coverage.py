"""Read-only integration coverage for an initialized empty workspace."""

from datetime import UTC, date, datetime

from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
    UniverseCoverageRequest,
)
from investment_analyst.workspace.service import WorkspaceService


def test_empty_workspace_reports_configured_sources_as_missing(tmp_path) -> None:
    workspace = WorkspaceService().initialize(tmp_path / "workspace").paths.root
    request = UniverseCoverageRequest(
        known_at=datetime(2026, 8, 29, tzinfo=UTC),
        market_start=date(2026, 8, 1),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2020, 1, 1),
        fundamental_end=date(2026, 8, 28),
        asset_ids=("crypto:sol-usd", "equity:us:msft", "etf:us:spy"),
    )

    result = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        request,
    )

    assert result.schema_version == "universe-coverage-v1"
    assert len(result.catalog_sha256) == 64
    assert [item.asset_id for item in result.assets] == list(request.asset_ids)
    assert all(item.market.capability is CoverageCapability.SUPPORTED for item in result.assets)
    assert all(item.market.evidence is EvidenceState.MISSING for item in result.assets)
    assert result.assets[0].market.volume_unit == "SOL"
    assert result.assets[1].corporate_valuation is CoverageCapability.NOT_CONFIGURED
    assert result.assets[2].fundamentals is CoverageCapability.NOT_APPLICABLE
