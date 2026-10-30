"""End-to-end explicit annual filing selection over a real scratch v2 workspace."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.valuation import (
    CorporateValuationFilingQuery,
    CorporateValuationFilingService,
    CorporateValuationRequest,
    CorporateValuationSelectedFilingRequest,
    CorporateValuationService,
)
from investment_analyst.analytics.valuation.service import (
    AmbiguousValuationEvidenceError,
)
from investment_analyst.application.analysis_capabilities import analysis_capabilities_for
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.fundamentals.sec_fact_models import get_sec_fact_definition
from investment_analyst.providers.market.alpaca_normalizer import (
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_FACTS = (
    ("fundamental.revenue", "100000", "USD"),
    ("fundamental.net_income", "20000", "USD"),
    ("fundamental.stockholders_equity", "80000", "USD"),
    ("fundamental.shares_outstanding", "1000", "shares"),
    ("fundamental.commercial_paper", "5000", "USD"),
    ("fundamental.long_term_debt_current", "2000", "USD"),
    ("fundamental.long_term_debt_noncurrent", "18000", "USD"),
    ("fundamental.cash_and_cash_equivalents", "20000", "USD"),
    ("fundamental.operating_income", "25000", "USD"),
    ("fundamental.operating_cash_flow", "30000", "USD"),
    ("fundamental.capital_expenditures", "10000", "USD"),
    ("fundamental.depreciation_and_amortization", "5000", "USD"),
)
_TSM_AVAILABLE = datetime(2026, 4, 17, tzinfo=UTC)
_TSM_BAR = datetime(2026, 4, 17, 16, tzinfo=UTC)


def _v2_storage(root: Path) -> LocalStorage:
    initialization = WorkspaceService().initialize(root, format_version=2)
    paths = StoragePaths.from_workspace_root(
        initialization.paths.root,
        format_version=initialization.manifest.format_version,
        workspace_id=initialization.manifest.workspace_id,
    )
    return LocalStorage(paths)


def _seed_facts(
    storage: LocalStorage,
    *,
    asset_id: str,
    source_id: str,
    available_at: datetime,
    accession_number: str,
    period_end: date,
    fiscal_year: str,
    form: str = "20-F",
    values: tuple[tuple[str, str, str], ...] = _FACTS,
) -> None:
    period_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    observations: list[NormalizedObservation] = []
    raw_records: list[RawRecord] = []
    for field_name, value, unit in values:
        definition = get_sec_fact_definition(field_name)
        raw_id = uuid4()
        key = {
            "accession_number": accession_number,
            "taxonomy": definition.taxonomy,
            "tag": definition.tag,
            "unit": unit,
            "period": period_end.isoformat(),
            "companyfacts_record_id": str(raw_id),
            "submissions_record_id": str(uuid4()),
            "form": form,
            "fiscal_year": fiscal_year,
            "fiscal_period": "FY",
        }
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=raw_id,
                asset_id=asset_id,
                field_name=field_name,
                value=Decimal(value),
                unit=unit,
                frequency=DataFrequency.ANNUAL,
                observed_at=period_at,
                period_end=period_at,
                available_at=available_at,
                normalized_at=available_at,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(key, separators=(",", ":"), sort_keys=True),
                    "retrieved_at": available_at,
                },
                quality=DataQuality.VALID,
                transformation_version="sec-companyfacts-normalizer-v2",
            )
        )
        raw_records.append(
            RawRecord(
                record_id=raw_id,
                asset_id=asset_id,
                source={
                    "source_id": source_id,
                    "record_key": json.dumps(
                        {"companyfacts_record_id": str(raw_id)},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "retrieved_at": available_at,
                },
                event_time=period_at,
                available_at=available_at,
                received_at=available_at,
                payload={"fixture": field_name},
                schema_version="sec-companyfacts-snapshot-v1",
            )
        )
    storage.raw_records.save_many(raw_records)
    storage.observations.save_many(observations)


def _configuration(catalog: AssetCatalogService, asset_id: str) -> AlpacaAssetConfiguration:
    asset = catalog.get(asset_id)
    configuration = resolve_alpaca_configuration(
        ProviderAssetContextResolver(catalog), asset_id=asset_id
    )
    return AlpacaAssetConfiguration(
        asset_id=asset.asset_id,
        symbol=asset.symbol,
        feed="iex",
        adjustment="all",
        source_id=configuration.source_id,
        name=asset.name,
        asset_class=asset.asset_class,
        quote_currency=asset.quote_currency,
        exchange=asset.exchange or "NYSE",
    )


def _seed_bar(
    storage: LocalStorage,
    *,
    catalog: AssetCatalogService,
    asset_id: str,
    timestamp: datetime,
    close: str = "100",
) -> None:
    configuration = _configuration(catalog, asset_id)
    close_value = Decimal(close)
    bar = AlpacaStockBar(
        symbol=configuration.symbol,
        timestamp=timestamp,
        open=close_value - Decimal("1"),
        high=close_value + Decimal("2"),
        low=close_value - Decimal("2"),
        close=close_value,
        volume=Decimal("1000"),
        trade_count=Decimal("10"),
        vwap=close_value,
        raw_values={
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(close_value - Decimal("1")),
            "h": str(close_value + Decimal("2")),
            "l": str(close_value - Decimal("2")),
            "c": str(close_value),
            "v": "1000",
            "n": "10",
            "vw": str(close_value),
        },
    )
    retrieved_at = timestamp + timedelta(hours=2)
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/filing-fixture",
        configuration=configuration,
    )
    storage.raw_records.save(raw)
    storage.observations.save_many(
        bar_to_observations(
            bar,
            raw,
            normalized_at=retrieved_at + timedelta(minutes=1),
            configuration=configuration,
        )
    )


def _services(
    storage: LocalStorage,
    catalog: AssetCatalogService,
    asset_id: str,
) -> tuple[CorporateValuationService, CorporateValuationFilingService]:
    asset = catalog.get(asset_id)
    resolver = ProviderAssetContextResolver(catalog)
    market_source_id = resolve_alpaca_configuration(resolver, asset_id=asset_id).source_id
    fundamental_source_id = resolve_sec_configuration(
        resolver, asset_id=asset_id
    ).companyfacts_source_id
    common = {
        "capabilities": analysis_capabilities_for(asset),
        "market_source_id": market_source_id,
        "fundamental_source_id": fundamental_source_id,
        "price_currency": asset.quote_currency,
        "security_unit_factor": asset.security_unit_factor,
        "security_unit_basis": asset.security_unit_basis,
        "security_unit_basis_version": asset.security_unit_basis_version,
        "security_unit_market_adjustment": asset.security_unit_market_adjustment,
        "security_unit_basis_revisions": asset.security_unit_basis_revisions,
    }
    return (
        CorporateValuationService(storage, **common),
        CorporateValuationFilingService(storage, **common),
    )


def _seed_two_cycles(storage: LocalStorage, catalog: AssetCatalogService) -> None:
    source_id = resolve_sec_configuration(
        ProviderAssetContextResolver(catalog), asset_id="equity:us:tsm"
    ).companyfacts_source_id
    _seed_facts(
        storage,
        asset_id="equity:us:tsm",
        source_id=source_id,
        available_at=datetime(2025, 2, 15, 12, tzinfo=UTC),
        accession_number="0001193125-25-000001",
        period_end=date(2024, 12, 31),
        fiscal_year="2024",
    )
    _seed_facts(
        storage,
        asset_id="equity:us:tsm",
        source_id=source_id,
        available_at=datetime(2025, 2, 15, 16, tzinfo=UTC),
        accession_number="0001193125-25-000001",
        period_end=date(2024, 12, 31),
        fiscal_year="2024",
    )
    _seed_facts(
        storage,
        asset_id="equity:us:tsm",
        source_id=source_id,
        available_at=datetime(2026, 1, 30, 12, tzinfo=UTC),
        accession_number="0001193125-26-000002",
        period_end=date(2025, 12, 31),
        fiscal_year="2025",
    )
    _seed_bar(storage, catalog=catalog, asset_id="equity:us:tsm", timestamp=_TSM_BAR)


def _filing_request(**updates: object) -> CorporateValuationSelectedFilingRequest:
    payload: dict[str, object] = {
        "asset_id": "equity:us:tsm",
        "known_at": datetime(2026, 5, 1, 20, tzinfo=UTC),
        "valuation_date": date(2026, 4, 17),
        "annual_period_end": date(2024, 12, 31),
        "filing_accession_number": "0001193125-25-000001",
        "filing_accepted_at": datetime(2025, 2, 15, 16, tzinfo=UTC),
        "filing_form": "20-F",
        "fiscal_year": "2024",
        "fiscal_period": "FY",
    }
    payload.update(updates)
    return CorporateValuationSelectedFilingRequest.model_validate(payload)


def _metric(snapshot: object, key: str) -> object:
    return next(item for item in snapshot.metrics if item.metric_key == key)


def test_options_separate_availabilities_and_preserve_latest_annual(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    request = CorporateValuationRequest(
        asset_id="equity:us:tsm",
        known_at=datetime(2026, 5, 1, 20, tzinfo=UTC),
        valuation_date=date(2026, 4, 17),
    )
    with storage:
        _seed_two_cycles(storage, catalog)
        _, filing_service = _services(storage, catalog, "equity:us:tsm")
        options = filing_service.query_options(
            CorporateValuationFilingQuery(
                asset_id="equity:us:tsm",
                known_at=datetime(2026, 5, 1, 20, tzinfo=UTC),
                valuation_date=date(2026, 4, 17),
                start_date=date(2024, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )
        assert options.total == 3
        assert options.returned == 3
        assert [item.selector.annual_period_end for item in options.options] == [
            date(2025, 12, 31),
            date(2024, 12, 31),
            date(2024, 12, 31),
        ]
        assert [item.selector.filing_accepted_at for item in options.options] == [
            datetime(2026, 1, 30, 12, tzinfo=UTC),
            datetime(2025, 2, 15, 16, tzinfo=UTC),
            datetime(2025, 2, 15, 12, tzinfo=UTC),
        ]
        assert "revenue" in options.options[0].available_field_roles
        assert options.options[0].taxonomies == ("us-gaap",)

        latest, _ = _services(storage, catalog, "equity:us:tsm")
        snapshot = latest.query(request, computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC))
        assert snapshot.annual_period_end == datetime(2025, 12, 31, tzinfo=UTC)
        assert snapshot.coverage.evaluated == 11


def test_selected_context_values_identities_and_price_cut(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    with storage:
        _seed_two_cycles(storage, catalog)
        _, filing_service = _services(storage, catalog, "equity:us:tsm")
        first = filing_service.query(
            _filing_request(),
            computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC),
        )
        assert first.schema_version == "corporate-valuation-selected-filing-snapshot-v1"
        assert first.annual_period_end == datetime(2024, 12, 31, tzinfo=UTC)
        assert first.filing_accepted_at == datetime(2025, 2, 15, 16, tzinfo=UTC)
        assert first.valuation_as_of == _TSM_BAR
        assert first.available_at == _TSM_BAR + timedelta(hours=2)
        assert first.security_basis is not None
        assert first.security_basis.market_units_per_reported_share == Decimal("0.2")
        assert _metric(first, "valuation.corporate.market_cap").value == Decimal("20000")
        assert _metric(
            first, "valuation.corporate.price_to_earnings_latest_annual"
        ).value == Decimal("1")
        assert _metric(first, "valuation.corporate.price_to_book").value == Decimal("0.25")
        assert _metric(first, "valuation.corporate.price_to_sales_latest_annual").value == Decimal(
            "0.2"
        )
        assert all(
            definition.algorithm_version
            == "corporate-valuation-selected-annual-filing-v1-decimal34"
            for definition in first.definitions
        )
        assert all(
            definition.definition_version
            == "corporate-valuation-selected-annual-filing-definition-v1"
            for definition in first.definitions
        )
        assert any(
            "Ejercicio anual elegido explícitamente" in limitation
            for limitation in first.limitations
        )

        second = filing_service.query(
            _filing_request(),
            computed_at=datetime(2026, 5, 1, 21, 5, tzinfo=UTC),
        )
        assert tuple(item.result_id for item in first.metrics) == tuple(
            item.result_id for item in second.metrics
        )
        assert tuple(item.value for item in first.metrics) == tuple(
            item.value for item in second.metrics
        )
        assert all(item.result_id.version == 8 for item in first.metrics)

        latest_request = CorporateValuationRequest(
            asset_id="equity:us:tsm",
            known_at=datetime(2026, 5, 1, 20, tzinfo=UTC),
            valuation_date=date(2026, 4, 17),
        )
        latest, _ = _services(storage, catalog, "equity:us:tsm")
        latest_snapshot = latest.query(
            latest_request, computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC)
        )
        selected_2025 = filing_service.query(
            _filing_request(
                annual_period_end=date(2025, 12, 31),
                filing_accession_number="0001193125-26-000002",
                filing_accepted_at=datetime(2026, 1, 30, 12, tzinfo=UTC),
                fiscal_year="2025",
            ),
            computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC),
        )
        assert selected_2025.coverage.evaluated == 11
        assert set(item.result_id for item in selected_2025.metrics).isdisjoint(
            item.result_id for item in latest_snapshot.metrics
        )


def test_invisible_or_unknown_selection_never_substitutes_another_filing(
    tmp_path: Path,
) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    with storage:
        _seed_two_cycles(storage, catalog)
        _, filing_service = _services(storage, catalog, "equity:us:tsm")
        unknown = filing_service.query(
            _filing_request(filing_accession_number="0001193125-99-999999"),
            computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC),
        )
        assert unknown.status.value == "not_evaluable"
        assert {item.reason_code.value for item in unknown.metrics} == {"fundamentals_unavailable"}
        assert unknown.annual_period_end is None

        with pytest.raises(ValidationError, match="filing_accepted_at must not be after"):
            _filing_request(known_at=datetime(2025, 2, 15, 14, tzinfo=UTC))


def test_contradictory_duplicate_inside_selected_context_fails_closed(
    tmp_path: Path,
) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    with storage:
        _seed_two_cycles(storage, catalog)
        source_id = resolve_sec_configuration(
            ProviderAssetContextResolver(catalog), asset_id="equity:us:tsm"
        ).companyfacts_source_id
        _seed_facts(
            storage,
            asset_id="equity:us:tsm",
            source_id=source_id,
            available_at=datetime(2024, 12, 31, tzinfo=UTC),
            accession_number="0001193125-24-000003",
            period_end=date(2023, 12, 31),
            fiscal_year="2023",
            values=(("fundamental.revenue", "100000", "USD"),),
        )
        conflicting = [
            item
            for item in storage.observations.list(
                asset_id="equity:us:tsm",
                source_id=source_id,
                field_names=("fundamental.revenue",),
                frequency=DataFrequency.ANNUAL,
            )
            if item.period_end is not None and item.period_end.date() == date(2023, 12, 31)
        ]
        assert len(conflicting) == 1
        storage.observations.save(
            conflicting[0].model_copy(
                update={
                    "observation_id": uuid4(),
                    "value": Decimal("999999"),
                }
            )
        )
        _, filing_service = _services(storage, catalog, "equity:us:tsm")
        request = _filing_request(
            annual_period_end=date(2023, 12, 31),
            filing_accession_number="0001193125-24-000003",
            filing_accepted_at=datetime(2024, 12, 31, tzinfo=UTC),
            fiscal_year="2023",
        )
        with pytest.raises(AmbiguousValuationEvidenceError):
            filing_service.query(
                request,
                computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC),
            )


def test_selected_query_is_read_only(tmp_path: Path) -> None:
    catalog = AssetCatalogService.load_default()
    storage = _v2_storage(tmp_path / "workspace")
    with storage:
        _seed_two_cycles(storage, catalog)
        _, filing_service = _services(storage, catalog, "equity:us:tsm")
        before_rows = {
            "raw_records": len(storage.raw_records.list(asset_id="equity:us:tsm")),
            "observations": len(storage.observations.list(asset_id="equity:us:tsm")),
            "metric_results": len(storage.metric_results.list_ids(asset_id="equity:us:tsm")),
            "metric_definitions": len(storage.metric_definitions.list_all()),
        }
        filing_service.query(_filing_request(), computed_at=datetime(2026, 5, 1, 21, tzinfo=UTC))
        filing_service.query_options(
            CorporateValuationFilingQuery(
                asset_id="equity:us:tsm",
                known_at=datetime(2026, 5, 1, 20, tzinfo=UTC),
                valuation_date=date(2026, 4, 17),
                start_date=date(2024, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )
        after_rows = {
            "raw_records": len(storage.raw_records.list(asset_id="equity:us:tsm")),
            "observations": len(storage.observations.list(asset_id="equity:us:tsm")),
            "metric_results": len(storage.metric_results.list_ids(asset_id="equity:us:tsm")),
            "metric_definitions": len(storage.metric_definitions.list_all()),
        }
        assert after_rows == before_rows
