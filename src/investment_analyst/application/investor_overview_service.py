"""Compose compact, independent per-asset projections from canonical readers."""

from datetime import datetime
from decimal import Decimal

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedSectionStatus,
    ListedCompanyDiagnosticRequest,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    ListedCompanyConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.statistics_definitions import SIMPLE_RETURN_KEY
from investment_analyst.analytics.valuation.models import (
    CorporateValuationSnapshot,
    ValuationMetricValue,
    ValuationStatus,
)
from investment_analyst.application.investor_overview_models import (
    InvestorFundamentalView,
    InvestorMarketView,
    InvestorOverviewAsset,
    InvestorOverviewRequest,
    InvestorOverviewResult,
    InvestorOverviewWindows,
    InvestorReferences,
    InvestorValuationView,
    InvestorValue,
    InvestorValueState,
)
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    build_market_asset_universe,
)
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    UniverseCoverageRequest,
)
from investment_analyst.application.universe_coverage_service import (
    UniverseCoverageService,
    _catalog_sha256,
    _limitations,
    fundamental_frequency_supported,
)
from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextResolver,
    ProviderAssetNotConfiguredError,
    ProviderCapabilityMissingError,
    ProviderNamespaceMissingError,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import (
    AssetClass,
    DataFrequency,
    DiagnosticVerdict,
    MetricResult,
)
from investment_analyst.storage import LocalStorage

NET_MARGIN_KEY = "fundamental.net_margin"
LIABILITIES_TO_ASSETS_KEY = "fundamental.liabilities_to_assets"
PRICE_TO_EARNINGS_KEY = "valuation.corporate.price_to_earnings_latest_annual"
PRICE_TO_BOOK_KEY = "valuation.corporate.price_to_book"
ENTERPRISE_VALUE_TO_EBITDA_KEY = "valuation.corporate.enterprise_value_to_ebitda_latest_annual"
SCORE_UNIT = "score_0_100"


class InvestorOverviewIntegrityError(RuntimeError):
    """Raised when selected evidence is ambiguous or violates the market contract."""


def _absent(
    state: InvestorValueState,
    *reasons: str,
    reference_at: datetime | None = None,
) -> InvestorValue:
    return InvestorValue(state=state, reason_codes=tuple(reasons), reference_at=reference_at)


class InvestorOverviewService:
    """Read one bounded batch once, without providers, writes or foreign hydration."""

    def __init__(
        self,
        storage: LocalStorage,
        catalog: AssetCatalogService,
        resolver: ProviderAssetContextResolver,
    ) -> None:
        self._storage = storage
        self._catalog = catalog
        self._resolver = resolver
        self._coverage = UniverseCoverageService(storage, catalog, resolver)
        self._diagnostics = ListedCompanyConsolidatedDiagnosticService(storage)

    def query(self, request: InvestorOverviewRequest) -> InvestorOverviewResult:
        """Return one stable-ordered projection for the explicit asset batch."""
        catalog_assets = {item.asset_id: item for item in self._catalog.list_assets()}
        unknown = tuple(item for item in request.asset_ids if item not in catalog_assets)
        if unknown:
            raise ValueError(f"asset is not configured in the catalog: {unknown[0]}")
        descriptors = {
            item.asset_id: item
            for item in build_market_asset_universe(self._catalog, self._resolver).assets
        }
        coverage_request = UniverseCoverageRequest(
            known_at=request.known_at,
            market_start=request.market_start,
            market_end=request.market_end,
            fundamental_start=request.fundamental_start,
            fundamental_end=request.market_end,
            frequency="annual",
            asset_ids=request.asset_ids,
        )
        assets = tuple(
            self._asset(
                catalog_assets[asset_id],
                descriptors.get(asset_id),
                request,
                coverage_request,
            )
            for asset_id in request.asset_ids
        )
        return InvestorOverviewResult(
            known_at=request.known_at,
            catalog_version=self._catalog.catalog_version,
            catalog_sha256=_catalog_sha256(self._catalog),
            windows=InvestorOverviewWindows(
                market_start=request.market_start,
                market_end=request.market_end,
                fundamental_start=request.fundamental_start,
                fundamental_end=request.market_end,
            ),
            assets=assets,
        )

    def _asset(
        self,
        asset: CatalogAsset,
        descriptor: MarketAssetDescriptor | None,
        request: InvestorOverviewRequest,
        coverage_request: UniverseCoverageRequest,
    ) -> InvestorOverviewAsset:
        market_coverage, bar = self._coverage.selected_market(
            descriptor, coverage_request, self._storage
        )
        return InvestorOverviewAsset(
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            name=asset.name,
            asset_class=asset.asset_class,
            exchange=asset.exchange or "UNKNOWN",
            quote_currency=asset.quote_currency,
            market=self._market_view(
                asset,
                request,
                bar,
                configured=market_coverage.capability is CoverageCapability.SUPPORTED,
                source_id=market_coverage.source_id,
            ),
            fundamental=self._fundamental_view(asset, request),
            valuation=self._valuation_view(asset, coverage_request, bar),
            limitations=_limitations(asset, descriptor),
        )

    def _market_view(
        self,
        asset: CatalogAsset,
        request: InvestorOverviewRequest,
        bar: MarketBar | None,
        *,
        configured: bool,
        source_id: str | None,
    ) -> InvestorMarketView:
        if not configured:
            absent = _absent(InvestorValueState.NOT_CONFIGURED, "market_not_configured")
            return InvestorMarketView(
                close=absent, change_1d=absent, currency=asset.quote_currency, source_id=source_id
            )
        if bar is None:
            missing = _absent(InvestorValueState.MISSING, "no_bar_in_window")
            return InvestorMarketView(
                close=missing, change_1d=missing, currency=asset.quote_currency, source_id=source_id
            )
        close = InvestorValue(
            state=InvestorValueState.AVAILABLE,
            value=bar.close,
            unit=asset.quote_currency,
            reference_at=bar.timestamp,
            available_at=bar.available_at,
            references=InvestorReferences(
                raw_record_id=bar.raw_record_id,
                observation_ids=(bar.observation_ids["close"],),
            ),
        )
        return InvestorMarketView(
            close=close,
            change_1d=self._change_1d(asset, request, bar),
            currency=asset.quote_currency,
            source_id=bar.source_id,
        )

    def _change_1d(
        self,
        asset: CatalogAsset,
        request: InvestorOverviewRequest,
        bar: MarketBar,
    ) -> InvestorValue:
        results = self._storage.metric_results.list(
            asset_id=asset.asset_id,
            metric_key=SIMPLE_RETURN_KEY,
            as_of_from=bar.timestamp,
            as_of_to=bar.timestamp,
        )
        compatible = [
            item
            for item in results
            if item.available_at <= request.known_at
            and item.as_of == bar.timestamp
            and item.parameters.get("source_id") == bar.source_id
            and item.unit == "ratio"
            and bool(item.input_observation_ids)
            and item.input_observation_ids[-1] == bar.observation_ids["close"]
        ]
        if not compatible:
            return _absent(
                InvestorValueState.MISSING,
                "simple_return_1d_not_derived_for_selected_bar",
                reference_at=bar.timestamp,
            )
        latest_available = max(item.available_at for item in compatible)
        latest = [item for item in compatible if item.available_at == latest_available]
        if len({(item.value, item.unit) for item in latest}) != 1:
            raise InvestorOverviewIntegrityError(
                f"ambiguous {SIMPLE_RETURN_KEY} revisions for {asset.asset_id}"
            )
        chosen = min(latest, key=lambda item: (item.computed_at, str(item.result_id)))
        return InvestorValue(
            state=InvestorValueState.AVAILABLE,
            value=chosen.value,
            unit="ratio",
            reference_at=chosen.as_of,
            available_at=chosen.available_at,
            references=InvestorReferences(
                metric_result_id=chosen.result_id,
                observation_ids=tuple(chosen.input_observation_ids),
            ),
        )

    def _fundamental_view(
        self,
        asset: CatalogAsset,
        request: InvestorOverviewRequest,
    ) -> InvestorFundamentalView:
        gate = self._fundamental_gate(asset)
        if gate is not None:
            return InvestorFundamentalView(score=gate, net_margin=gate, liabilities_to_assets=gate)
        section, metric_index = self._diagnostics.query_fundamental_selection(
            ListedCompanyDiagnosticRequest(
                asset_id=asset.asset_id,
                known_at=request.known_at,
                fundamental_frequency=DataFrequency.ANNUAL,
            )
        )
        window_start = request.fundamental_start
        diagnostic = section.diagnostic
        if section.status is ConsolidatedSectionStatus.NOT_FOUND or diagnostic is None:
            missing = _absent(InvestorValueState.MISSING, "no_annual_diagnostic_at_known_at")
            return InvestorFundamentalView(
                score=missing, net_margin=missing, liabilities_to_assets=missing
            )
        if diagnostic.as_of.date() < window_start:
            outside = _absent(
                InvestorValueState.MISSING,
                "annual_diagnostic_outside_window",
                reference_at=diagnostic.as_of,
            )
            return InvestorFundamentalView(
                score=outside, net_margin=outside, liabilities_to_assets=outside
            )
        references = InvestorReferences(diagnostic_id=diagnostic.diagnostic_id)
        if diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
            score = InvestorValue(
                state=InvestorValueState.INSUFFICIENT_DATA,
                reason_codes=("diagnostic_insufficient_data",),
                reference_at=diagnostic.as_of,
                available_at=diagnostic.available_at,
                references=references,
            )
        else:
            score = InvestorValue(
                state=InvestorValueState.AVAILABLE,
                value=diagnostic.final_score,
                unit=SCORE_UNIT,
                reference_at=diagnostic.as_of,
                available_at=diagnostic.available_at,
                references=references,
            )
        selected = [metric_index[item] for item in section.selected_metric_result_ids]
        return InvestorFundamentalView(
            score=score,
            verdict=diagnostic.verdict.value,
            algorithm_version=diagnostic.algorithm_version,
            net_margin=_selected_metric(selected, NET_MARGIN_KEY),
            liabilities_to_assets=_selected_metric(selected, LIABILITIES_TO_ASSETS_KEY),
        )

    def _fundamental_gate(self, asset: CatalogAsset) -> InvestorValue | None:
        if asset.asset_class is not AssetClass.EQUITY:
            return _absent(InvestorValueState.NOT_APPLICABLE, "asset_class_has_no_fundamentals")
        try:
            configuration = resolve_sec_configuration(self._resolver, asset_id=asset.asset_id)
        except (
            ProviderAssetNotConfiguredError,
            ProviderCapabilityMissingError,
            ProviderNamespaceMissingError,
        ):
            return _absent(InvestorValueState.NOT_CONFIGURED, "fundamentals_not_configured")
        if not fundamental_frequency_supported(configuration, "annual"):
            return _absent(InvestorValueState.NOT_SUPPORTED, "annual_frequency_not_supported")
        return None

    def _valuation_view(
        self,
        asset: CatalogAsset,
        coverage_request: UniverseCoverageRequest,
        bar: MarketBar | None,
    ) -> InvestorValuationView:
        if asset.asset_class is not AssetClass.EQUITY:
            gate = _absent(InvestorValueState.NOT_APPLICABLE, "asset_class_has_no_valuation")
            return InvestorValuationView(
                price_to_earnings=gate, price_to_book=gate, enterprise_value_to_ebitda=gate
            )
        snapshot = self._coverage.valuation_snapshot(
            asset, coverage_request, self._storage, selected_price=bar
        )
        if snapshot is None:
            gate = _absent(InvestorValueState.NOT_CONFIGURED, "valuation_not_configured")
            return InvestorValuationView(
                price_to_earnings=gate, price_to_book=gate, enterprise_value_to_ebitda=gate
            )
        by_key = {item.metric_key: item for item in snapshot.metrics}
        return InvestorValuationView(
            status=snapshot.status.value,
            annual_period_end=snapshot.annual_period_end,
            price_date=snapshot.valuation_as_of,
            price_currency=snapshot.price_currency,
            price_to_earnings=_valuation_value(snapshot, by_key.get(PRICE_TO_EARNINGS_KEY)),
            price_to_book=_valuation_value(snapshot, by_key.get(PRICE_TO_BOOK_KEY)),
            enterprise_value_to_ebitda=_valuation_value(
                snapshot, by_key.get(ENTERPRISE_VALUE_TO_EBITDA_KEY)
            ),
        )


def _selected_metric(selected: list[MetricResult], metric_key: str) -> InvestorValue:
    matches = [item for item in selected if item.metric_key == metric_key]
    if not matches:
        return _absent(InvestorValueState.MISSING, "metric_not_in_selected_diagnostic")
    if len(matches) != 1:
        raise InvestorOverviewIntegrityError(f"ambiguous selected metric {metric_key}")
    metric = matches[0]
    return InvestorValue(
        state=InvestorValueState.AVAILABLE,
        value=metric.value,
        unit=metric.unit,
        reference_at=metric.as_of,
        available_at=metric.available_at,
        references=InvestorReferences(
            metric_result_id=metric.result_id,
            observation_ids=tuple(metric.input_observation_ids),
        ),
    )


def _valuation_value(
    snapshot: CorporateValuationSnapshot,
    metric: ValuationMetricValue | None,
) -> InvestorValue:
    if metric is None:
        raise InvestorOverviewIntegrityError("valuation snapshot lost a published metric")
    unit = "ratio"
    if metric.status is ValuationStatus.EVALUATED:
        value: Decimal | None = metric.value
        return InvestorValue(
            state=InvestorValueState.AVAILABLE,
            value=value,
            unit=unit,
            reference_at=snapshot.valuation_as_of,
            available_at=metric.available_at,
            references=InvestorReferences(
                metric_result_id=metric.result_id,
                observation_ids=metric.input_observation_ids,
                filing_accession_number=snapshot.filing_accession_number,
            ),
        )
    state = (
        InvestorValueState.NOT_APPLICABLE
        if metric.status is ValuationStatus.NOT_APPLICABLE
        else InvestorValueState.INSUFFICIENT_DATA
    )
    reasons = (metric.reason_code.value,) if metric.reason_code is not None else ()
    return InvestorValue(
        state=state,
        reason_codes=reasons,
        reference_at=snapshot.valuation_as_of,
    )


__all__ = ["InvestorOverviewIntegrityError", "InvestorOverviewService"]
