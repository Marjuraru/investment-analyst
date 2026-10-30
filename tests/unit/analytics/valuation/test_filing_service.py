"""Focused service tests for explicit annual filing selection."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.valuation import (
    CorporateValuationFilingQuery,
    CorporateValuationFilingService,
    CorporateValuationSelectedFilingRequest,
)
from investment_analyst.analytics.valuation.service import (
    CorporateValuationError,
    MalformedValuationEvidenceError,
)
from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisCapabilities,
    analysis_capabilities_for,
)
from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextError,
    ProviderAssetContextResolver,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import DataFrequency, DataQuality, NormalizedObservation
from investment_analyst.providers.fundamentals.sec_fact_models import get_sec_fact_definition

_KNOWN_AT = datetime(2026, 2, 1, tzinfo=UTC)
_ACCEPTED_AT = datetime(2025, 2, 15, 16, tzinfo=UTC)
_VALUATION_DATE = date(2025, 6, 30)
_PRICE_AT = datetime(2025, 6, 30, 16, tzinfo=UTC)
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


class _ObservationRepository:
    def __init__(self, rows: list[NormalizedObservation]) -> None:
        self.rows = rows
        self.calls: list[dict[str, object]] = []

    def list(self, **filters: object) -> list[NormalizedObservation]:
        self.calls.append(filters)
        return list(self.rows)


class _Storage:
    def __init__(self, rows: list[NormalizedObservation] | None = None) -> None:
        self.observations = _ObservationRepository(rows or [])
        self.open_calls = 0

    def require_open(self) -> None:
        self.open_calls += 1


def _observation(
    field_name: str,
    value: str,
    unit: str,
    *,
    asset_id: str = "equity:us:aapl",
    source_id: str,
    available_at: datetime = _ACCEPTED_AT,
    period_end: date = date(2024, 12, 31),
    period_start: date | None = None,
    accession_number: str = "0000320193-25-000079",
    form: str = "10-K",
    fiscal_year: str = "2024",
    fiscal_period: str = "FY",
    taxonomy: str | None = None,
    record_key: str | None = None,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    quality: DataQuality = DataQuality.VALID,
) -> NormalizedObservation:
    definition = (
        get_sec_fact_definition(field_name)
        if taxonomy is None
        else get_sec_fact_definition(field_name, taxonomy=taxonomy)
    )
    period_at = datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)
    key = {
        "accession_number": accession_number,
        "taxonomy": definition.taxonomy,
        "tag": definition.tag,
        "unit": unit,
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(uuid4()),
        "submissions_record_id": str(uuid4()),
        "form": form,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
    }
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=uuid4(),
        asset_id=asset_id,
        field_name=field_name,
        value=Decimal(value),
        unit=unit,
        frequency=frequency,
        observed_at=period_at,
        period_start=(
            datetime.combine(period_start, datetime.min.time(), tzinfo=UTC)
            if period_start is not None
            else None
        ),
        period_end=period_at,
        available_at=available_at,
        normalized_at=available_at,
        source={
            "source_id": source_id,
            "record_key": record_key
            if record_key is not None
            else json.dumps(key, separators=(",", ":"), sort_keys=True),
            "retrieved_at": available_at,
        },
        quality=quality,
        transformation_version="sec-companyfacts-normalizer-v2",
    )


def _full_facts(
    *,
    source_id: str,
    available_at: datetime = _ACCEPTED_AT,
    period_end: date = date(2024, 12, 31),
    accession_number: str = "0000320193-25-000079",
    fiscal_year: str = "2024",
) -> list[NormalizedObservation]:
    return [
        _observation(
            field_name,
            value,
            unit,
            source_id=source_id,
            available_at=available_at,
            period_end=period_end,
            accession_number=accession_number,
            fiscal_year=fiscal_year,
        )
        for field_name, value, unit in _FACTS
    ]


def _context(
    asset_id: str = "equity:us:aapl",
) -> tuple[CatalogAsset, AssetAnalysisCapabilities, str | None, str | None]:
    catalog = AssetCatalogService.load_default()
    resolver = ProviderAssetContextResolver(catalog)
    asset = catalog.get(asset_id)
    capabilities = analysis_capabilities_for(asset)
    try:
        market_source_id = resolve_alpaca_configuration(resolver, asset_id=asset_id).source_id
    except (ValueError, ProviderAssetContextError):
        market_source_id = None
    try:
        fundamental_source_id = resolve_sec_configuration(
            resolver, asset_id=asset_id
        ).companyfacts_source_id
    except (ValueError, ProviderAssetContextError):
        fundamental_source_id = None
    return (
        asset,
        capabilities,
        market_source_id,
        fundamental_source_id,
    )


def _service(
    storage: _Storage,
    *,
    asset_id: str = "equity:us:aapl",
) -> CorporateValuationFilingService:
    asset, capabilities, market_source_id, fundamental_source_id = _context(asset_id)
    return CorporateValuationFilingService(
        storage,
        capabilities=capabilities,
        market_source_id=market_source_id,
        fundamental_source_id=fundamental_source_id,
        price_currency=asset.quote_currency,
        security_unit_factor=asset.security_unit_factor,
        security_unit_basis=asset.security_unit_basis,
        security_unit_basis_version=asset.security_unit_basis_version,
        security_unit_market_adjustment=asset.security_unit_market_adjustment,
        security_unit_basis_revisions=asset.security_unit_basis_revisions,
    )


def _source_id(asset_id: str = "equity:us:aapl") -> str:
    source_id = _context(asset_id)[3]
    assert source_id is not None
    return source_id


def _market_source_id(asset_id: str = "equity:us:aapl") -> str:
    source_id = _context(asset_id)[2]
    assert source_id is not None
    return source_id


def _price(asset_id: str = "equity:us:aapl") -> MarketBar:
    return MarketBar(
        asset_id=asset_id,
        source_id=_market_source_id(asset_id),
        raw_record_id=uuid4(),
        frequency=DataFrequency.DAY_1,
        timestamp=_PRICE_AT,
        available_at=_PRICE_AT + timedelta(hours=2),
        open=Decimal("99"),
        high=Decimal("102"),
        low=Decimal("98"),
        close=Decimal("100"),
        volume=Decimal("1000"),
        quality=DataQuality.VALID,
        observation_ids={
            "open": uuid4(),
            "high": uuid4(),
            "low": uuid4(),
            "close": uuid4(),
            "volume": uuid4(),
        },
    )


def _request(
    *,
    asset_id: str = "equity:us:aapl",
    accession_number: str = "0000320193-25-000079",
    accepted_at: datetime = _ACCEPTED_AT,
    period_end: date = date(2024, 12, 31),
    fiscal_year: str = "2024",
) -> CorporateValuationSelectedFilingRequest:
    return CorporateValuationSelectedFilingRequest(
        asset_id=asset_id,
        known_at=_KNOWN_AT,
        valuation_date=_VALUATION_DATE,
        annual_period_end=period_end,
        filing_accession_number=accession_number,
        filing_accepted_at=accepted_at,
        filing_form="10-K",
        fiscal_year=fiscal_year,
        fiscal_period="FY",
    )


def test_options_group_by_exact_tuple_and_bound_the_repository_scope() -> None:
    source_id = _source_id()
    storage = _Storage(
        [
            *_full_facts(source_id=source_id),
            _observation(
                "fundamental.revenue",
                "10",
                "USD",
                source_id=source_id,
                available_at=datetime(2025, 2, 15, 20, tzinfo=UTC),
            ),
            *_full_facts(
                source_id=source_id,
                available_at=datetime(2026, 1, 30, 12, tzinfo=UTC),
                period_end=date(2025, 12, 31),
                accession_number="0000320193-26-000080",
                fiscal_year="2025",
            ),
        ]
    )
    service = _service(storage)
    query = CorporateValuationFilingQuery(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        valuation_date=date(2026, 2, 1),
        start_date=date(2024, 1, 1),
        end_date=date(2026, 12, 31),
        limit=2,
    )

    page = service.query_options(query)
    assert page.total == 3
    assert page.returned == 2
    assert [item.selector.annual_period_end for item in page.options] == [
        date(2025, 12, 31),
        date(2024, 12, 31),
    ]
    assert page.options[0].selector.filing_accepted_at == datetime(2026, 1, 30, 12, tzinfo=UTC)
    assert page.options[1].selector.filing_accepted_at == datetime(2025, 2, 15, 20, tzinfo=UTC)
    assert page.options[1].available_field_roles == ("revenue",)
    assert "shares_outstanding" in page.options[0].available_field_roles

    second = service.query_options(
        CorporateValuationFilingQuery.model_validate(
            {**query.model_dump(), "offset": 2, "limit": 2}
        )
    )
    assert second.returned == 1
    assert second.options[0].selector.filing_accepted_at == _ACCEPTED_AT

    call = storage.observations.calls[0]
    assert call["asset_id"] == "equity:us:aapl"
    assert call["source_id"] == source_id
    assert call["frequency"] is DataFrequency.ANNUAL
    assert call["quality"] is DataQuality.VALID
    assert call["available_to"] == _KNOWN_AT
    assert "fundamental.revenue" in call["field_names"]  # type: ignore[operator]

    future_cut = CorporateValuationFilingQuery(
        asset_id="equity:us:aapl",
        known_at=_KNOWN_AT,
        valuation_date=date(2025, 6, 30),
        start_date=date(2024, 1, 1),
        end_date=date(2026, 12, 31),
    )
    assert service.query_options(future_cut).total == 2
    assert (
        service.query_options(
            CorporateValuationFilingQuery.model_validate(
                {**future_cut.model_dump(), "valuation_date": date(2025, 12, 31)}
            )
        ).total
        == 3
    )


def test_selected_context_evaluates_only_its_own_literal_group() -> None:
    source_id = _source_id()
    storage = _Storage(
        [
            *_full_facts(source_id=source_id),
            _observation(
                "fundamental.revenue",
                "10",
                "USD",
                source_id=source_id,
                available_at=datetime(2025, 2, 15, 20, tzinfo=UTC),
            ),
        ]
    )
    service = _service(storage)
    snapshot = service.query(
        _request(),
        computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        selected_price=_price(),
    )
    metrics = {item.metric_key: item for item in snapshot.metrics}
    assert snapshot.coverage.evaluated == 11
    assert metrics["valuation.corporate.market_cap"].value == Decimal("100000")
    assert metrics["valuation.corporate.price_to_earnings_latest_annual"].value == Decimal("5")
    assert metrics["valuation.corporate.price_to_book"].value == Decimal("1.25")
    assert metrics["valuation.corporate.price_to_sales_latest_annual"].value == Decimal("1")
    assert snapshot.available_at == _PRICE_AT + timedelta(hours=2)
    assert all(item.result_id.version == 8 for item in snapshot.metrics)

    corrected = service.query(
        _request(accepted_at=datetime(2025, 2, 15, 20, tzinfo=UTC)),
        computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        selected_price=_price(),
    )
    corrected_metrics = {item.metric_key: item for item in corrected.metrics}
    assert corrected_metrics["valuation.corporate.market_cap"].status.value == "not_evaluable"
    assert corrected_metrics["valuation.corporate.market_cap"].reason_code.value == (
        "missing_input"
    )
    assert corrected.coverage.evaluated < snapshot.coverage.evaluated

    unknown = service.query(
        _request(accession_number="0000320193-99-999999"),
        computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        selected_price=_price(),
    )
    assert unknown.status.value == "not_evaluable"
    assert {item.reason_code.value for item in unknown.metrics} == {"fundamentals_unavailable"}
    assert unknown.annual_period_end is None


def test_asset_mismatch_malformed_metadata_and_shared_semantics_fail_closed() -> None:
    source_id = _source_id()
    service = _service(_Storage(_full_facts(source_id=source_id)))
    with pytest.raises(CorporateValuationError, match="asset_id does not match"):
        service.query(_request(asset_id="equity:us:amd"), selected_price=_price())

    malformed = _Storage(
        [
            _observation(
                "fundamental.revenue",
                "100",
                "USD",
                source_id=source_id,
                record_key="{}",
            )
        ]
    )
    with pytest.raises(MalformedValuationEvidenceError):
        _service(malformed).query_options(
            CorporateValuationFilingQuery(
                asset_id="equity:us:aapl",
                known_at=_KNOWN_AT,
                valuation_date=_VALUATION_DATE,
                start_date=date(2024, 1, 1),
                end_date=date(2025, 12, 31),
            )
        )

    mismatched_period = _Storage(
        [
            _observation(
                "fundamental.revenue",
                "100000",
                "USD",
                source_id=source_id,
                period_start=date(2024, 1, 1),
            ),
            _observation(
                "fundamental.net_income",
                "20000",
                "USD",
                source_id=source_id,
                period_start=date(2024, 2, 1),
            ),
        ]
    )
    period_snapshot = _service(mismatched_period).query(
        _request(),
        computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        selected_price=_price(),
    )
    assert {item.reason_code.value for item in period_snapshot.metrics} == {"period_mismatch"}

    currency_mismatch = _Storage(
        [
            _observation(
                "fundamental.revenue",
                "100000",
                "EUR",
                source_id=source_id,
            ),
            _observation(
                "fundamental.shares_outstanding",
                "1000",
                "shares",
                source_id=source_id,
            ),
        ]
    )
    currency_snapshot = _service(currency_mismatch).query(
        _request(),
        computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        selected_price=_price(),
    )
    assert {item.reason_code.value for item in currency_snapshot.metrics} == {"currency_mismatch"}

    unit_mismatch = _Storage(
        [
            _observation(
                "fundamental.shares_outstanding",
                "1000",
                "ADS",
                source_id=source_id,
            ),
        ]
    )
    unit_snapshot = _service(unit_mismatch).query(
        _request(),
        computed_at=datetime(2026, 2, 1, 21, tzinfo=UTC),
        selected_price=_price(),
    )
    assert {item.reason_code.value for item in unit_snapshot.metrics} == {"unit_mismatch"}


def test_non_corporate_asset_has_no_contexts_and_is_not_applicable() -> None:
    service = _service(_Storage(), asset_id="crypto:btc-usd")
    options = service.query_options(
        CorporateValuationFilingQuery(
            asset_id="crypto:btc-usd",
            known_at=_KNOWN_AT,
            valuation_date=_VALUATION_DATE,
            start_date=date(2024, 1, 1),
            end_date=date(2025, 12, 31),
        )
    )
    assert options.total == 0
    assert options.options == ()
    snapshot = service.query(_request(asset_id="crypto:btc-usd"))
    assert snapshot.status.value == "not_applicable"
    assert {item.reason_code.value for item in snapshot.metrics} == {"asset_not_applicable"}
