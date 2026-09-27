"""Read-only integration coverage for an initialized empty workspace."""

import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
    UniverseCoverageRequest,
)
from investment_analyst.core.models import DataFrequency, DataQuality, NormalizedObservation
from investment_analyst.storage import LocalStorage, StoragePaths
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
    assert result.assets[1].corporate_valuation.capability is CoverageCapability.SUPPORTED
    assert result.assets[1].corporate_valuation.evidence is EvidenceState.MISSING
    assert result.assets[2].fundamentals.capability is CoverageCapability.NOT_APPLICABLE


def test_default_query_keeps_bvl_identities_when_market_is_not_configured(tmp_path) -> None:
    workspace = WorkspaceService().initialize(tmp_path / "workspace").paths.root
    request = UniverseCoverageRequest(
        known_at=datetime(2026, 8, 29, tzinfo=UTC),
        market_start=date(2026, 8, 1),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2020, 1, 1),
        fundamental_end=date(2026, 8, 28),
    )

    result = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        request,
    )

    assert len(result.assets) == 37
    bvl = next(item for item in result.assets if item.asset_id == "equity:pe:bvl:bvn")
    assert bvl.market.capability is CoverageCapability.NOT_CONFIGURED
    assert bvl.bvl_registry.capability is CoverageCapability.SUPPORTED
    assert bvl.bvl_registry.evidence is EvidenceState.MISSING


def test_valuation_reasons_distinguish_common_shares_from_adr_in_coverage(tmp_path) -> None:
    workspace = WorkspaceService().initialize(tmp_path / "workspace").paths.root
    request = UniverseCoverageRequest(
        known_at=datetime(2026, 8, 29, tzinfo=UTC),
        market_start=date(2026, 8, 1),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2020, 1, 1),
        fundamental_end=date(2026, 8, 28),
        asset_ids=(
            "equity:us:aapl",
            "equity:us:amd",
            "equity:us:bvn",
            "equity:us:msft",
            "equity:us:tsm",
        ),
    )

    result = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=workspace),
        request,
    )

    assets_by_id = {item.asset_id: item for item in result.assets}
    for common_id in ("equity:us:aapl", "equity:us:amd", "equity:us:msft"):
        cov = assets_by_id[common_id].corporate_valuation
        assert cov.capability is CoverageCapability.SUPPORTED
        assert "share_basis_unavailable" not in cov.reason_codes

    for adr_id in ("equity:us:bvn", "equity:us:tsm"):
        cov = assets_by_id[adr_id].corporate_valuation
        assert cov.capability is CoverageCapability.SUPPORTED
        assert "share_basis_unavailable" in cov.reason_codes


def _sec_observation(
    *,
    asset_id: str,
    source_id: str,
    transformation_version: str,
    field_name: str,
    value: str,
    period_end: date,
    available_at: datetime,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    unit: str | None = None,
) -> NormalizedObservation:
    from investment_analyst.providers.fundamentals.sec_fact_models import (
        get_sec_fact_definition,
    )

    definition = get_sec_fact_definition(field_name)
    resolved_unit = unit or definition.unit
    raw_id = uuid4()
    key = {
        "accession_number": "0000320193-25-000079",
        "taxonomy": definition.taxonomy,
        "tag": definition.tag,
        "unit": resolved_unit,
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(raw_id),
        "submissions_record_id": str(uuid4()),
        "form": "10-K",
        "fiscal_year": "2025",
        "fiscal_period": "FY",
    }
    observed_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    period_end_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_id,
        asset_id=asset_id,
        field_name=field_name,
        value=Decimal(value),
        unit=resolved_unit,
        frequency=frequency,
        observed_at=observed_at,
        period_end=period_end_at,
        available_at=available_at,
        normalized_at=max(available_at, datetime(2025, 11, 1, tzinfo=UTC)),
        source={
            "source_id": source_id,
            "record_key": json.dumps(key, separators=(",", ":"), sort_keys=True),
            "retrieved_at": max(available_at, datetime(2025, 11, 1, tzinfo=UTC)),
        },
        quality=DataQuality.VALID,
        transformation_version=transformation_version,
    )


def _seed_scoped_matrix(storage: LocalStorage) -> dict[str, datetime]:
    early = datetime(2026, 8, 29, tzinfo=UTC)
    late = datetime(2026, 9, 5, tzinfo=UTC)
    period = date(2025, 9, 27)
    rows: list[NormalizedObservation] = []
    for asset_id, source_id, transformation_version in (
        (
            "equity:us:aapl",
            "sec-edgar:aapl:companyfacts",
            "sec-aapl-companyfacts-normalizer-v1",
        ),
        ("equity:us:amd", "sec-edgar:amd:companyfacts", "sec-companyfacts-normalizer-v2"),
    ):
        for field_name, value in (
            ("fundamental.revenue", "1000"),
            ("fundamental.net_income", "200"),
            ("fundamental.stockholders_equity", "800"),
            ("fundamental.shares_outstanding", "100"),
            ("fundamental.commercial_paper", "50"),
            ("fundamental.long_term_debt_current", "20"),
            ("fundamental.long_term_debt_noncurrent", "180"),
            ("fundamental.cash_and_cash_equivalents", "200"),
            ("fundamental.operating_income", "250"),
            ("fundamental.operating_cash_flow", "300"),
            ("fundamental.capital_expenditures", "100"),
        ):
            rows.append(
                _sec_observation(
                    asset_id=asset_id,
                    source_id=source_id,
                    transformation_version=transformation_version,
                    field_name=field_name,
                    value=value,
                    period_end=period,
                    available_at=early,
                )
            )
        rows.append(
            _sec_observation(
                asset_id=asset_id,
                source_id=source_id,
                transformation_version=transformation_version,
                field_name="fundamental.revenue",
                value="1000",
                period_end=period,
                available_at=late,
            )
        )
    for _ in range(400):
        rows.append(
            _sec_observation(
                asset_id="equity:us:aapl",
                source_id="sec-edgar:aapl:companyfacts",
                transformation_version="sec-aapl-companyfacts-normalizer-v1",
                field_name="fundamental.revenue",
                value="1",
                period_end=date(2020, 1, 1),
                available_at=datetime(2026, 8, 1, tzinfo=UTC),
                frequency=DataFrequency.QUARTERLY,
            )
        )
    rows.append(
        _sec_observation(
            asset_id="etf:us:spy",
            source_id="sec-edgar:spy:companyfacts",
            transformation_version="sec-companyfacts-normalizer-v2",
            field_name="fundamental.revenue",
            value="5",
            period_end=period,
            available_at=early,
        )
    )
    storage.observations.save_many(rows)
    return {"early": early, "late": late}


def _matrix_request(known_at: datetime) -> UniverseCoverageRequest:
    return UniverseCoverageRequest(
        known_at=known_at,
        market_start=date(2026, 8, 1),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2020, 1, 1),
        fundamental_end=known_at.date(),
        asset_ids=("crypto:btc-usd", "equity:us:aapl", "equity:us:amd", "etf:us:spy"),
    )


def test_scoped_sec_reads_preserve_universe_matrix_at_distinct_cuts(tmp_path) -> None:
    workspace = WorkspaceService().initialize(tmp_path / "workspace").paths.root
    storage_paths = StoragePaths.from_root(workspace / "storage")
    with LocalStorage(storage_paths) as storage:
        cuts = _seed_scoped_matrix(storage)
        total = storage.observations.count(asset_id="equity:us:aapl")
        scoped = storage.observations.count(
            asset_id="equity:us:aapl",
            source_id="sec-edgar:aapl:companyfacts",
            frequency=DataFrequency.ANNUAL,
            quality=DataQuality.VALID,
            available_to=cuts["early"],
        )
        assert total >= 400
        assert scoped <= total // 5

    application = UniverseCoverageApplication.create_default()
    before_early = application.query(
        StorageLocationRequest(workspace=workspace), _matrix_request(cuts["early"])
    )
    before_late = application.query(
        StorageLocationRequest(workspace=workspace), _matrix_request(cuts["late"])
    )
    start = time.perf_counter()
    after_early = application.query(
        StorageLocationRequest(workspace=workspace), _matrix_request(cuts["early"])
    )
    after_late = application.query(
        StorageLocationRequest(workspace=workspace), _matrix_request(cuts["late"])
    )
    elapsed = time.perf_counter() - start

    assert after_early.model_dump_json() == before_early.model_dump_json()
    assert after_late.model_dump_json() == before_late.model_dump_json()
    assert after_early.model_dump_json() != after_late.model_dump_json()
    by_id = {item.asset_id: item for item in after_early.assets}
    assert by_id["equity:us:aapl"].fundamentals.evidence is EvidenceState.PRESENT
    assert by_id["equity:us:amd"].fundamentals.evidence is EvidenceState.PRESENT
    assert by_id["etf:us:spy"].fundamentals.capability is CoverageCapability.NOT_APPLICABLE
    assert by_id["crypto:btc-usd"].fundamentals.capability is CoverageCapability.NOT_APPLICABLE
    assert elapsed >= 0
