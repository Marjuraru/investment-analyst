"""Focused latest-annual corporate valuation tests."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.valuation import (
    AmbiguousValuationEvidenceError,
    CorporateValuationRequest,
    CorporateValuationService,
    ValuationInput,
    ValuationReasonCode,
    ValuationSecurityBasis,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.application.analysis_capabilities import analysis_capabilities_for
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)

_ASSET_ID = "equity:us:aapl"
_KNOWN_AT = datetime(2026, 2, 1, tzinfo=UTC)
_AVAILABLE_AT = datetime(2025, 11, 1, tzinfo=UTC)
_PERIOD_END = datetime(2025, 9, 27, tzinfo=UTC)
_SOURCE = SourceReference(source_id="sec-edgar:aapl:companyfacts", retrieved_at=_AVAILABLE_AT)


class _Observations:
    def __init__(self, rows: list[NormalizedObservation]) -> None:
        self._rows = rows

    def list(self, *, asset_id: str | None = None) -> list[NormalizedObservation]:
        return [row for row in self._rows if asset_id is None or row.asset_id == asset_id]


class _Storage:
    def __init__(self, rows: list[NormalizedObservation]) -> None:
        self.observations = _Observations(rows)

    def require_open(self) -> None:
        return None


def _observation(
    field_name: str,
    value: str,
    *,
    asset_id: str = _ASSET_ID,
    available_at: datetime = _AVAILABLE_AT,
    period_start: datetime | None = None,
    period_end: datetime = _PERIOD_END,
    accession_number: str = "0000320193-25-000079",
    fiscal_year: int = 2025,
    unit: str | None = None,
    taxonomy: str = "us-gaap",
    source_id: str = _SOURCE.source_id,
    frequency: DataFrequency = DataFrequency.ANNUAL,
) -> NormalizedObservation:
    tag = field_name.removeprefix("fundamental.")
    resolved_unit = unit or ("shares" if field_name.endswith("shares_outstanding") else "USD")
    return NormalizedObservation(
        raw_record_id=uuid4(),
        asset_id=asset_id,
        field_name=field_name,
        value=Decimal(value),
        unit=resolved_unit,
        frequency=frequency,
        period_start=period_start,
        period_end=period_end,
        available_at=available_at,
        normalized_at=max(_KNOWN_AT, available_at),
        source=_SOURCE.model_copy(
            update={
                "source_id": source_id,
                "record_key": json.dumps(
                    {
                        "accession_number": accession_number,
                        "taxonomy": taxonomy,
                        "tag": tag,
                        "unit": resolved_unit,
                        "period": period_end.date().isoformat(),
                        "form": "10-K",
                        "fiscal_year": fiscal_year,
                        "fiscal_period": "FY",
                        "companyfacts_record_id": str(uuid4()),
                        "submissions_record_id": str(uuid4()),
                    },
                    sort_keys=True,
                ),
            }
        ),
        quality=DataQuality.VALID,
        transformation_version="test-v1",
    )


def _price() -> MarketBar:
    identifiers = {name: uuid4() for name in ("open", "high", "low", "close", "volume")}
    return MarketBar(
        asset_id=_ASSET_ID,
        source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        raw_record_id=uuid4(),
        frequency=DataFrequency.DAY_1,
        timestamp=datetime(2026, 1, 30, tzinfo=UTC),
        available_at=_AVAILABLE_AT,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=Decimal("1"),
        quality=DataQuality.VALID,
        observation_ids=identifiers,
    )


def _service(rows: list[NormalizedObservation]) -> CorporateValuationService:
    asset = AssetCatalogService.load_default().get(_ASSET_ID)
    return CorporateValuationService(
        _Storage(rows),
        capabilities=analysis_capabilities_for(asset),
        market_source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        fundamental_source_id=_SOURCE.source_id,
        price_currency="USD",
        security_unit_factor=asset.security_unit_factor,
        security_unit_basis=asset.security_unit_basis,
        security_unit_basis_version=asset.security_unit_basis_version,
        security_unit_market_adjustment=asset.security_unit_market_adjustment,
    )


def _reference_rows(
    *,
    available_at: datetime = _AVAILABLE_AT,
    accession_number: str = "0000320193-25-000079",
    **overrides: str,
) -> list[NormalizedObservation]:
    values = {
        "fundamental.shares_outstanding": "100",
        "fundamental.commercial_paper": "50",
        "fundamental.long_term_debt_current": "20",
        "fundamental.long_term_debt_noncurrent": "180",
        "fundamental.cash_and_cash_equivalents": "200",
        "fundamental.net_income": "200",
        "fundamental.stockholders_equity": "800",
        "fundamental.revenue": "1000",
        "fundamental.operating_income": "250",
        "fundamental.depreciation_and_amortization": "50",
        "fundamental.operating_cash_flow": "300",
        "fundamental.capital_expenditures": "100",
    }
    values.update(overrides)
    return [
        _observation(
            field_name,
            value,
            available_at=available_at,
            accession_number=accession_number,
        )
        for field_name, value in values.items()
    ]


def test_public_models_are_strict_versioned_and_reject_unsafe_numbers() -> None:
    request = CorporateValuationRequest(
        asset_id=_ASSET_ID,
        known_at=_KNOWN_AT,
        valuation_date=date(2026, 1, 30),
    )

    assert request.schema_version == "corporate-valuation-request-v1"
    with pytest.raises(ValidationError, match="Extra inputs"):
        CorporateValuationRequest.model_validate(
            {**request.model_dump(), "unexpected": "forbidden"}
        )
    for value in (True, 1.0, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValidationError):
            ValuationSecurityBasis(
                basis="reported_common_share",
                market_units_per_reported_share=value,
                market_adjustment="all",
                contract_version="security-unit-basis-v1",
            )
        with pytest.raises(ValidationError):
            ValuationInput(
                role="close_price",
                observation_id=uuid4(),
                raw_record_id=uuid4(),
                source_id="test:market",
                value=value,
                unit="USD",
                frequency=DataFrequency.DAY_1,
                observed_at=datetime(2026, 1, 30, tzinfo=UTC),
                available_at=_AVAILABLE_AT,
            )


def test_latest_annual_formulas_keep_negative_yields_and_missing_ebitda_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _observation("fundamental.shares_outstanding", "100"),
        _observation("fundamental.commercial_paper", "50"),
        _observation("fundamental.long_term_debt_current", "20"),
        _observation("fundamental.long_term_debt_noncurrent", "180"),
        _observation("fundamental.cash_and_cash_equivalents", "200"),
        _observation("fundamental.net_income", "200"),
        _observation("fundamental.stockholders_equity", "800"),
        _observation("fundamental.revenue", "1000"),
        _observation("fundamental.operating_income", "250"),
        _observation("fundamental.operating_cash_flow", "300"),
        _observation("fundamental.capital_expenditures", "100"),
    ]
    service = _service(rows)
    monkeypatch.setattr(service, "_price", lambda request: _price())

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    by_key = {item.metric_key: item for item in snapshot.metrics}
    assert by_key["valuation.corporate.market_cap"].value == Decimal("1000")
    assert by_key["valuation.corporate.financial_debt"].value == Decimal("250")
    assert by_key["valuation.corporate.enterprise_value"].value == Decimal("1050")
    assert by_key["valuation.corporate.price_to_earnings_latest_annual"].value == Decimal("5")
    assert by_key["valuation.corporate.free_cash_flow_yield_latest_annual"].value == Decimal("0.2")
    assert by_key["valuation.corporate.enterprise_value_to_ebitda_latest_annual"].status is (
        ValuationStatus.NOT_EVALUABLE
    )
    assert (
        by_key["valuation.corporate.enterprise_value_to_ebitda_latest_annual"].reason_code
        is ValuationReasonCode.EBITDA_UNAVAILABLE
    )


def test_all_reference_formulas_use_metric_specific_inputs() -> None:
    rows = _reference_rows()
    service = _service(rows)
    price = _price()
    service._price = lambda request: price  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    values = {item.metric_key: item.value for item in snapshot.metrics}
    assert values == {
        "valuation.corporate.earnings_yield_latest_annual": Decimal("0.2"),
        "valuation.corporate.enterprise_value": Decimal("1050"),
        "valuation.corporate.enterprise_value_to_ebit_latest_annual": Decimal("4.2"),
        "valuation.corporate.enterprise_value_to_ebitda_latest_annual": Decimal("3.5"),
        "valuation.corporate.enterprise_value_to_sales_latest_annual": Decimal("1.05"),
        "valuation.corporate.financial_debt": Decimal("250"),
        "valuation.corporate.free_cash_flow_yield_latest_annual": Decimal("0.2"),
        "valuation.corporate.market_cap": Decimal("1000"),
        "valuation.corporate.price_to_book": Decimal("1.25"),
        "valuation.corporate.price_to_earnings_latest_annual": Decimal("5"),
        "valuation.corporate.price_to_sales_latest_annual": Decimal("1"),
    }
    market_cap = next(item for item in snapshot.metrics if item.metric_key.endswith("market_cap"))
    assert len(market_cap.input_observation_ids) == 2
    assert snapshot.status is ValuationSnapshotStatus.EVALUATED
    assert snapshot.available_at == _AVAILABLE_AT
    assert snapshot.computed_at > snapshot.known_at
    replay = service.query(
        snapshot.request,
        computed_at=datetime(2026, 2, 3, tzinfo=UTC),
    )
    assert tuple(item.result_id for item in replay.metrics) == tuple(
        item.result_id for item in snapshot.metrics
    )
    assert snapshot.to_json_dict()["metrics"][0]["value"] == "0.2"


def test_negative_income_blocks_pe_but_preserves_signed_earnings_yield() -> None:
    rows = [
        _observation("fundamental.shares_outstanding", "100"),
        _observation("fundamental.net_income", "-20"),
        _observation("fundamental.revenue", "1000"),
        _observation("fundamental.stockholders_equity", "800"),
    ]
    service = _service(rows)
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    by_key = {item.metric_key: item for item in snapshot.metrics}
    assert by_key["valuation.corporate.price_to_earnings_latest_annual"].reason_code is (
        ValuationReasonCode.INVALID_DENOMINATOR
    )
    assert by_key["valuation.corporate.earnings_yield_latest_annual"].value == Decimal("-0.02")
    assert by_key["valuation.corporate.price_to_sales_latest_annual"].value == Decimal("1")


def test_missing_debt_component_is_never_converted_to_zero() -> None:
    rows = [item for item in _reference_rows() if item.field_name != "fundamental.commercial_paper"]
    service = _service(rows)
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )
    by_key = {item.metric_key: item for item in snapshot.metrics}

    assert by_key["valuation.corporate.market_cap"].value == Decimal("1000")
    assert by_key["valuation.corporate.financial_debt"].reason_code is (
        ValuationReasonCode.MISSING_INPUT
    )
    assert by_key["valuation.corporate.enterprise_value"].reason_code is (
        ValuationReasonCode.MISSING_INPUT
    )


def test_latest_revision_enters_only_after_its_acceptance() -> None:
    revision_at = datetime(2026, 2, 2, tzinfo=UTC)
    old = _observation("fundamental.net_income", "100")
    revised = _observation(
        "fundamental.net_income",
        "200",
        available_at=revision_at,
        accession_number="0000320193-26-000002",
    )
    rows = [
        _observation("fundamental.shares_outstanding", "100"),
        old,
        revised,
    ]
    before_service = _service(rows)
    after_service = _service(rows)
    before_service._price = lambda request: _price()  # type: ignore[method-assign]
    after_service._price = lambda request: _price()  # type: ignore[method-assign]

    before = before_service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )
    after = after_service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=datetime(2026, 2, 3, tzinfo=UTC),
            valuation_date=datetime(2026, 2, 3, tzinfo=UTC).date(),
        ),
        computed_at=datetime(2026, 2, 4, tzinfo=UTC),
    )

    before_income = next(item for item in before.inputs if item.role == "net_income")
    after_income = next(item for item in after.inputs if item.role == "net_income")
    assert before_income.value == Decimal("100")
    assert after_income.value == Decimal("200")
    assert before_income.observation_id != after_income.observation_id


def test_distinct_complete_revision_changes_deterministic_result_identity() -> None:
    revision_at = datetime(2026, 2, 2, tzinfo=UTC)
    rows = [
        *_reference_rows(),
        *_reference_rows(
            available_at=revision_at,
            accession_number="0000320193-26-000002",
        ),
    ]
    service = _service(rows)
    service._price = lambda request: _price()  # type: ignore[method-assign]

    before = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=datetime(2026, 2, 1, 1, tzinfo=UTC),
    )
    after = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=datetime(2026, 2, 3, tzinfo=UTC),
            valuation_date=date(2026, 2, 3),
        ),
        computed_at=datetime(2026, 2, 4, tzinfo=UTC),
    )

    assert before.coverage.evaluated == after.coverage.evaluated == 11
    assert tuple(item.value for item in before.metrics) == tuple(
        item.value for item in after.metrics
    )
    assert set(item.result_id for item in before.metrics).isdisjoint(
        item.result_id for item in after.metrics
    )


def test_equally_available_semantic_revisions_fail_closed() -> None:
    rows = [
        _observation("fundamental.net_income", "100"),
        _observation(
            "fundamental.net_income",
            "200",
            accession_number="0000320193-25-000080",
        ),
    ]
    service = _service(rows)
    service._price = lambda request: _price()  # type: ignore[method-assign]

    with pytest.raises(AmbiguousValuationEvidenceError, match="semantically"):
        service.query(
            CorporateValuationRequest(
                asset_id=_ASSET_ID,
                known_at=_KNOWN_AT,
                valuation_date=_KNOWN_AT.date(),
            ),
            computed_at=_KNOWN_AT,
        )


def test_mixed_annual_duration_starts_are_not_evaluable() -> None:
    rows = [
        _observation(
            "fundamental.revenue",
            "1000",
            period_start=datetime(2024, 9, 29, tzinfo=UTC),
        ),
        _observation(
            "fundamental.net_income",
            "100",
            period_start=datetime(2024, 10, 1, tzinfo=UTC),
        ),
    ]
    service = _service(rows)
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    assert {item.reason_code for item in snapshot.metrics} == {ValuationReasonCode.PERIOD_MISMATCH}


def test_currency_mismatch_is_explicit_and_does_not_assume_fx() -> None:
    rows = [
        _observation("fundamental.shares_outstanding", "100"),
        _observation("fundamental.revenue", "1000", unit="EUR"),
    ]
    service = _service(rows)
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    assert snapshot.status is ValuationSnapshotStatus.NOT_EVALUABLE
    assert {item.reason_code for item in snapshot.metrics} == {
        ValuationReasonCode.CURRENCY_MISMATCH
    }


def test_share_unit_and_accounting_taxonomy_mismatches_fail_closed() -> None:
    bad_share_service = _service(
        [
            _observation("fundamental.shares_outstanding", "100", unit="USD"),
            _observation("fundamental.revenue", "1000"),
        ]
    )
    bad_taxonomy_service = _service(
        [
            _observation("fundamental.shares_outstanding", "100"),
            _observation("fundamental.revenue", "1000", taxonomy="ifrs-full"),
        ]
    )
    for service in (bad_share_service, bad_taxonomy_service):
        service._price = lambda request: _price()  # type: ignore[method-assign]
    request = CorporateValuationRequest(
        asset_id=_ASSET_ID,
        known_at=_KNOWN_AT,
        valuation_date=_KNOWN_AT.date(),
    )

    bad_share = bad_share_service.query(request, computed_at=_KNOWN_AT)
    bad_taxonomy = bad_taxonomy_service.query(request, computed_at=_KNOWN_AT)

    assert {item.reason_code for item in bad_share.metrics} == {ValuationReasonCode.UNIT_MISMATCH}
    assert {item.reason_code for item in bad_taxonomy.metrics} == {
        ValuationReasonCode.ACCOUNTING_BASIS_MISMATCH
    }


def test_wrong_source_and_frequency_facts_are_not_mixed_into_the_filing() -> None:
    service = _service(
        [
            _observation("fundamental.shares_outstanding", "100"),
            _observation(
                "fundamental.revenue",
                "1000",
                source_id="sec-edgar:other:companyfacts",
            ),
            _observation(
                "fundamental.net_income",
                "200",
                frequency=DataFrequency.QUARTERLY,
            ),
        ]
    )
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )
    by_key = {item.metric_key: item for item in snapshot.metrics}

    assert {item.role for item in snapshot.inputs} == {"close_price", "shares_outstanding"}
    assert by_key["valuation.corporate.market_cap"].status is ValuationStatus.EVALUATED
    assert by_key["valuation.corporate.price_to_sales_latest_annual"].reason_code is (
        ValuationReasonCode.MISSING_INPUT
    )
    assert by_key["valuation.corporate.price_to_earnings_latest_annual"].reason_code is (
        ValuationReasonCode.MISSING_INPUT
    )


@pytest.mark.parametrize(
    ("field_name", "value", "metric_key"),
    [
        ("fundamental.shares_outstanding", "0", "valuation.corporate.market_cap"),
        ("fundamental.stockholders_equity", "0", "valuation.corporate.price_to_book"),
        (
            "fundamental.revenue",
            "-1",
            "valuation.corporate.enterprise_value_to_sales_latest_annual",
        ),
        (
            "fundamental.operating_income",
            "0",
            "valuation.corporate.enterprise_value_to_ebit_latest_annual",
        ),
        (
            "fundamental.depreciation_and_amortization",
            "-250",
            "valuation.corporate.enterprise_value_to_ebitda_latest_annual",
        ),
    ],
)
def test_nonpositive_multiple_denominators_are_not_evaluable(
    field_name: str,
    value: str,
    metric_key: str,
) -> None:
    service = _service(_reference_rows(**{field_name: value}))
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    metric = next(item for item in snapshot.metrics if item.metric_key == metric_key)
    assert metric.reason_code is ValuationReasonCode.INVALID_DENOMINATOR


def test_missing_security_basis_and_stale_price_are_explicit() -> None:
    catalog = AssetCatalogService.load_default()
    foreign = catalog.get("equity:us:bvn")
    unavailable_service = CorporateValuationService(
        _Storage([]),
        capabilities=analysis_capabilities_for(foreign),
        market_source_id="alpaca-market-data:iex:bvn:daily-bars:adjustment-all",
        fundamental_source_id="sec-edgar:bvn:companyfacts",
        price_currency="USD",
        security_unit_factor=None,
    )
    unavailable = unavailable_service.query(
        CorporateValuationRequest(
            asset_id=foreign.asset_id,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )
    assert {item.reason_code for item in unavailable.metrics} == {
        ValuationReasonCode.SHARE_BASIS_UNAVAILABLE
    }

    stale_service = _service(_reference_rows())
    stale_service._price = lambda request: _price().model_copy(  # type: ignore[method-assign]
        update={"timestamp": datetime(2026, 1, 20, tzinfo=UTC)}
    )
    stale = stale_service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )
    assert stale.price_age_days == 12
    assert any("retrasado" in limitation for limitation in stale.limitations)
    assert any("IEX" in limitation and "SIP" in limitation for limitation in stale.limitations)


@pytest.mark.parametrize(
    ("capability_update", "source_update", "reason"),
    [
        (
            {"market_data_configured": False},
            {"market_source_id": None},
            ValuationReasonCode.MARKET_NOT_CONFIGURED,
        ),
        (
            {"fundamental_data_configured": False},
            {"fundamental_source_id": None},
            ValuationReasonCode.FUNDAMENTALS_NOT_CONFIGURED,
        ),
    ],
)
def test_missing_configured_sources_are_explicit(
    capability_update: dict[str, object],
    source_update: dict[str, object],
    reason: ValuationReasonCode,
) -> None:
    asset = AssetCatalogService.load_default().get(_ASSET_ID)
    arguments: dict[str, object] = {
        "capabilities": analysis_capabilities_for(asset).model_copy(update=capability_update),
        "market_source_id": "alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        "fundamental_source_id": _SOURCE.source_id,
        "price_currency": "USD",
        "security_unit_factor": asset.security_unit_factor,
        "security_unit_basis": asset.security_unit_basis,
        "security_unit_basis_version": asset.security_unit_basis_version,
        "security_unit_market_adjustment": asset.security_unit_market_adjustment,
    }
    arguments.update(source_update)
    service = CorporateValuationService(_Storage([]), **arguments)

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    assert {item.reason_code for item in snapshot.metrics} == {reason}


def test_cut_before_filing_acceptance_does_not_select_future_facts() -> None:
    service = _service(_reference_rows(available_at=datetime(2026, 2, 2, tzinfo=UTC)))
    service._price = lambda request: _price()  # type: ignore[method-assign]

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id=_ASSET_ID,
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    assert snapshot.inputs == ()
    assert {item.reason_code for item in snapshot.metrics} == {
        ValuationReasonCode.FUNDAMENTALS_UNAVAILABLE
    }


def test_crypto_asset_is_not_applicable_without_reading_price() -> None:
    asset = AssetCatalogService.load_default().get("crypto:btc-usd")
    service = CorporateValuationService(
        _Storage([]),
        capabilities=analysis_capabilities_for(asset),
        market_source_id="coinbase:btc-usd:86400s:candles",
        fundamental_source_id=None,
        price_currency="USD",
        security_unit_factor=None,
    )

    snapshot = service.query(
        CorporateValuationRequest(
            asset_id="crypto:btc-usd",
            known_at=_KNOWN_AT,
            valuation_date=_KNOWN_AT.date(),
        ),
        computed_at=_KNOWN_AT,
    )

    assert snapshot.status is ValuationSnapshotStatus.NOT_APPLICABLE
    assert {item.reason_code for item in snapshot.metrics} == {
        ValuationReasonCode.ASSET_NOT_APPLICABLE
    }
