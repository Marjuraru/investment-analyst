"""Read-only latest-annual corporate valuation from persisted evidence."""

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext
from typing import Protocol

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery, MarketBar
from investment_analyst.analytics.market.history_service import (
    AmbiguousRevisionError,
    HistoricalMarketDataService,
)
from investment_analyst.analytics.valuation.identity import valuation_result_id
from investment_analyst.analytics.valuation.models import (
    CorporateValuationRequest,
    CorporateValuationSnapshot,
    ValuationCoverage,
    ValuationInput,
    ValuationMetricDefinition,
    ValuationMetricValue,
    ValuationReasonCode,
    ValuationSecurityBasis,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisCapabilities,
    AssetAnalysisFamily,
    FundamentalAnalysisMode,
)
from investment_analyst.core.models import DataFrequency, DataQuality, NormalizedObservation

_ALGORITHM = "corporate-valuation-latest-annual-v1-decimal34"
_DEFINITION_VERSION = "corporate-valuation-definition-v1"
_LIMITATION = "Base latest_annual; no es TTM, forward, consenso, DCF ni recomendación."
_IEX_LIMITATION = "Precio diario Alpaca IEX: cobertura de una bolsa, no SIP consolidado."
_SHARE_LIMITATION = (
    "Las acciones corresponden al cierre del ejercicio anual; no se infieren recompras, "
    "emisiones ni conversiones posteriores."
)

_MONETARY_FIELDS = frozenset(
    {
        "fundamental.commercial_paper",
        "fundamental.long_term_debt_current",
        "fundamental.long_term_debt_noncurrent",
        "fundamental.cash_and_cash_equivalents",
        "fundamental.net_income",
        "fundamental.stockholders_equity",
        "fundamental.revenue",
        "fundamental.operating_income",
        "fundamental.depreciation_and_amortization",
        "fundamental.operating_cash_flow",
        "fundamental.capital_expenditures",
    }
)
_FIELDS = _MONETARY_FIELDS | {"fundamental.shares_outstanding"}


class CorporateValuationError(RuntimeError):
    """Base error for a local corporate valuation query."""


class AmbiguousValuationEvidenceError(CorporateValuationError):
    """Raised when equally eligible revisions disagree semantically."""


class MalformedValuationEvidenceError(CorporateValuationError):
    """Raised when candidate SEC observations lack required audit metadata."""


class IncompatibleValuationEvidenceError(CorporateValuationError):
    """Identify coherent persisted evidence that cannot be combined safely."""

    def __init__(self, reason: ValuationReasonCode, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class _ObservationRepository(Protocol):
    def list(
        self,
        *,
        asset_id: str | None = None,
        **filters: object,
    ) -> list[NormalizedObservation]: ...


class _Storage(Protocol):
    observations: _ObservationRepository

    def require_open(self) -> None: ...


@dataclass(frozen=True)
class _FilingMetadata:
    accession_number: str
    taxonomy: str
    tag: str
    form: str
    fiscal_year: str
    fiscal_period: str


@dataclass(frozen=True)
class _AnnualSelection:
    facts: Mapping[str, NormalizedObservation]
    metadata: _FilingMetadata
    period_start: datetime | None
    period_end: datetime
    accepted_at: datetime


def _definition(
    key: str,
    name: str,
    formula: str,
    roles: tuple[str, ...],
    unit: str,
    *limitations: str,
) -> ValuationMetricDefinition:
    published_limitations = (
        _LIMITATION,
        *limitations,
        *((_IEX_LIMITATION,) if "close_price" in roles else ()),
    )
    return ValuationMetricDefinition(
        metric_key=key,
        display_name_es=name,
        formula=formula,
        input_roles=roles,
        unit=unit,
        algorithm_version=_ALGORITHM,
        definition_version=_DEFINITION_VERSION,
        limitations=published_limitations,
    )


_DEFINITIONS = tuple(
    sorted(
        (
            _definition(
                "valuation.corporate.market_cap",
                "Capitalización bursátil",
                "close_price * shares_outstanding * market_units_per_reported_share",
                ("close_price", "shares_outstanding"),
                "USD",
                _SHARE_LIMITATION,
            ),
            _definition(
                "valuation.corporate.financial_debt",
                "Deuda financiera",
                "commercial_paper + long_term_debt_current + long_term_debt_noncurrent",
                ("commercial_paper", "long_term_debt_current", "long_term_debt_noncurrent"),
                "USD",
            ),
            _definition(
                "valuation.corporate.enterprise_value",
                "Enterprise value",
                "market_cap + financial_debt - cash_and_cash_equivalents",
                (
                    "close_price",
                    "shares_outstanding",
                    "commercial_paper",
                    "long_term_debt_current",
                    "long_term_debt_noncurrent",
                    "cash_and_cash_equivalents",
                ),
                "USD",
                "EV v1 no añade leases, preferred equity, minoritarios ni valores negociables.",
            ),
            _definition(
                "valuation.corporate.price_to_earnings_latest_annual",
                "Precio / utilidad anual",
                "market_cap / annual_net_income",
                ("close_price", "shares_outstanding", "net_income"),
                "ratio",
            ),
            _definition(
                "valuation.corporate.price_to_book",
                "Precio / valor en libros",
                "market_cap / stockholders_equity",
                ("close_price", "shares_outstanding", "stockholders_equity"),
                "ratio",
            ),
            _definition(
                "valuation.corporate.price_to_sales_latest_annual",
                "Precio / ventas anuales",
                "market_cap / annual_revenue",
                ("close_price", "shares_outstanding", "revenue"),
                "ratio",
            ),
            _definition(
                "valuation.corporate.enterprise_value_to_sales_latest_annual",
                "EV / ventas anuales",
                "enterprise_value / annual_revenue",
                (
                    "close_price",
                    "shares_outstanding",
                    "commercial_paper",
                    "long_term_debt_current",
                    "long_term_debt_noncurrent",
                    "cash_and_cash_equivalents",
                    "revenue",
                ),
                "ratio",
            ),
            _definition(
                "valuation.corporate.enterprise_value_to_ebit_latest_annual",
                "EV / EBIT anual",
                "enterprise_value / annual_operating_income",
                (
                    "close_price",
                    "shares_outstanding",
                    "commercial_paper",
                    "long_term_debt_current",
                    "long_term_debt_noncurrent",
                    "cash_and_cash_equivalents",
                    "operating_income",
                ),
                "ratio",
            ),
            _definition(
                "valuation.corporate.enterprise_value_to_ebitda_latest_annual",
                "EV / EBITDA anual",
                (
                    "enterprise_value / (annual_operating_income + "
                    "annual_depreciation_and_amortization)"
                ),
                (
                    "close_price",
                    "shares_outstanding",
                    "commercial_paper",
                    "long_term_debt_current",
                    "long_term_debt_noncurrent",
                    "cash_and_cash_equivalents",
                    "operating_income",
                    "depreciation_and_amortization",
                ),
                "ratio",
                (
                    "EBITDA solo se reconstruye con D&A anual oficial compatible; "
                    "nunca sustituye EBIT."
                ),
            ),
            _definition(
                "valuation.corporate.free_cash_flow_yield_latest_annual",
                "FCF yield anual",
                "(annual_operating_cash_flow - annual_capital_expenditures) / market_cap",
                (
                    "operating_cash_flow",
                    "capital_expenditures",
                    "close_price",
                    "shares_outstanding",
                ),
                "percentage",
            ),
            _definition(
                "valuation.corporate.earnings_yield_latest_annual",
                "Earnings yield anual",
                "annual_net_income / market_cap",
                ("net_income", "close_price", "shares_outstanding"),
                "percentage",
            ),
        ),
        key=lambda item: item.metric_key,
    )
)


class CorporateValuationService:
    """Compose stored daily prices and SEC facts without providers or writes."""

    def __init__(
        self,
        storage: _Storage,
        *,
        capabilities: AssetAnalysisCapabilities,
        market_source_id: str | None,
        fundamental_source_id: str | None,
        price_currency: str | None,
        security_unit_factor: Decimal | None,
        security_unit_basis: str | None = None,
        security_unit_basis_version: str | None = None,
        security_unit_market_adjustment: str | None = None,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._capabilities = capabilities
        self._market_source_id = market_source_id
        self._fundamental_source_id = fundamental_source_id
        self._price_currency = price_currency
        self._security_unit_factor = security_unit_factor
        self._security_unit_basis = security_unit_basis
        self._security_unit_basis_version = security_unit_basis_version
        self._security_unit_market_adjustment = security_unit_market_adjustment

    def query(
        self,
        request: CorporateValuationRequest,
        *,
        computed_at: datetime | None = None,
    ) -> CorporateValuationSnapshot:
        """Reconstruct one valuation using only evidence eligible at the requested cut."""
        self._storage.require_open()
        now = self._normalized_computed_at(computed_at)
        if request.asset_id != self._capabilities.asset_id:
            raise CorporateValuationError("valuation request asset_id does not match capabilities")
        unavailable = self._eligibility_reason()
        if unavailable is not None:
            status = (
                ValuationStatus.NOT_APPLICABLE
                if unavailable is ValuationReasonCode.ASSET_NOT_APPLICABLE
                else ValuationStatus.NOT_EVALUABLE
            )
            return self._unavailable(request, now, unavailable, status)
        security_basis = self._security_basis()
        if security_basis is None:
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.SHARE_BASIS_UNAVAILABLE,
                ValuationStatus.NOT_EVALUABLE,
            )
        try:
            price = self._price(request)
        except AmbiguousRevisionError as error:
            raise AmbiguousValuationEvidenceError(
                "eligible market price revisions are ambiguous"
            ) from error
        if price is None:
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.PRICE_UNAVAILABLE,
                ValuationStatus.NOT_EVALUABLE,
                security_basis=security_basis,
            )
        try:
            selection = self._annual_facts(request)
        except IncompatibleValuationEvidenceError as error:
            return self._unavailable(
                request,
                now,
                error.reason,
                ValuationStatus.NOT_EVALUABLE,
                security_basis=security_basis,
            )
        if selection is None:
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.FUNDAMENTALS_UNAVAILABLE,
                ValuationStatus.NOT_EVALUABLE,
                security_basis=security_basis,
            )
        report_currencies = self._report_currencies(selection.facts)
        report_currency = next(iter(report_currencies), None)
        if len(report_currencies) > 1 or (
            report_currency is not None and report_currency != self._price_currency
        ):
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.CURRENCY_MISMATCH,
                ValuationStatus.NOT_EVALUABLE,
                security_basis=security_basis,
            )
        shares = selection.facts.get("fundamental.shares_outstanding")
        if shares is not None and shares.unit != "shares":
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.UNIT_MISMATCH,
                ValuationStatus.NOT_EVALUABLE,
                security_basis=security_basis,
            )

        inputs = self._inputs(price, selection)
        by_role = {item.role: item for item in inputs}
        metrics = tuple(
            self._evaluate_metric(
                definition,
                request=request,
                price=price,
                selection=selection,
                security_basis=security_basis,
                inputs=by_role,
            )
            for definition in _DEFINITIONS
        )
        available_at = max(item.available_at for item in inputs)
        coverage = _coverage(metrics)
        status = _snapshot_status(coverage)
        limitations = [_IEX_LIMITATION, _SHARE_LIMITATION]
        if (request.valuation_date - price.timestamp.date()).days > 3:
            limitations.append(
                "El último precio elegible está retrasado respecto de valuation_date."
            )
        return CorporateValuationSnapshot(
            asset_id=request.asset_id,
            request=request,
            status=status,
            valuation_as_of=price.timestamp,
            known_at=request.known_at,
            computed_at=now,
            available_at=available_at,
            price_age_days=(request.valuation_date - price.timestamp.date()).days,
            annual_period_start=selection.period_start,
            annual_period_end=selection.period_end,
            filing_date=selection.accepted_at.date(),
            filing_accepted_at=selection.accepted_at,
            filing_accession_number=selection.metadata.accession_number,
            filing_form=selection.metadata.form,
            fiscal_year=selection.metadata.fiscal_year,
            fiscal_period=selection.metadata.fiscal_period,
            price_currency=self._price_currency,
            report_currency=report_currency,
            price_source_id=self._market_source_id,
            fundamental_source_id=self._fundamental_source_id,
            security_basis=security_basis,
            inputs=inputs,
            definitions=_DEFINITIONS,
            metrics=metrics,
            coverage=coverage,
            limitations=tuple(limitations),
        )

    @staticmethod
    def _normalized_computed_at(value: datetime | None) -> datetime:
        resolved = value or datetime.now(UTC)
        if resolved.tzinfo is None or resolved.utcoffset() is None:
            raise CorporateValuationError("computed_at must be timezone-aware")
        return resolved.astimezone(UTC)

    def _security_basis(self) -> ValuationSecurityBasis | None:
        values = (
            self._security_unit_factor,
            self._security_unit_basis,
            self._security_unit_basis_version,
            self._security_unit_market_adjustment,
        )
        if not all(value is not None for value in values):
            return None
        return ValuationSecurityBasis(
            basis=self._security_unit_basis,
            market_units_per_reported_share=self._security_unit_factor,
            market_adjustment=self._security_unit_market_adjustment,
            contract_version=self._security_unit_basis_version,
        )

    def _eligibility_reason(self) -> ValuationReasonCode | None:
        if (
            self._capabilities.family is not AssetAnalysisFamily.LISTED_COMPANY
            or self._capabilities.fundamental_mode is not FundamentalAnalysisMode.CORPORATE
        ):
            return ValuationReasonCode.ASSET_NOT_APPLICABLE
        if not self._capabilities.market_data_configured or self._market_source_id is None:
            return ValuationReasonCode.MARKET_NOT_CONFIGURED
        if (
            not self._capabilities.fundamental_data_configured
            or self._fundamental_source_id is None
        ):
            return ValuationReasonCode.FUNDAMENTALS_NOT_CONFIGURED
        return None

    def _unavailable(
        self,
        request: CorporateValuationRequest,
        now: datetime,
        reason: ValuationReasonCode,
        status: ValuationStatus,
        *,
        security_basis: ValuationSecurityBasis | None = None,
    ) -> CorporateValuationSnapshot:
        metrics = tuple(
            ValuationMetricValue(metric_key=item.metric_key, status=status, reason_code=reason)
            for item in _DEFINITIONS
        )
        return CorporateValuationSnapshot(
            asset_id=request.asset_id,
            request=request,
            status=(
                ValuationSnapshotStatus.NOT_APPLICABLE
                if status is ValuationStatus.NOT_APPLICABLE
                else ValuationSnapshotStatus.NOT_EVALUABLE
            ),
            known_at=request.known_at,
            computed_at=now,
            price_currency=self._price_currency,
            price_source_id=self._market_source_id,
            fundamental_source_id=self._fundamental_source_id,
            security_basis=security_basis,
            definitions=_DEFINITIONS,
            metrics=metrics,
            coverage=_coverage(metrics),
        )

    def _price(self, request: CorporateValuationRequest) -> MarketBar | None:
        start = datetime.combine(date(1970, 1, 1), datetime.min.time(), UTC)
        end = datetime.combine(request.valuation_date + timedelta(days=1), datetime.min.time(), UTC)
        bars = (
            HistoricalMarketDataService(self._storage)
            .query(
                HistoricalBarQuery(
                    asset_id=request.asset_id,
                    source_id=self._market_source_id or "",
                    start=start,
                    end=end,
                    known_at=request.known_at,
                )
            )
            .bars
        )
        return bars[-1] if bars else None

    def _annual_facts(self, request: CorporateValuationRequest) -> _AnnualSelection | None:
        candidates = [
            item
            for item in self._storage.observations.list(asset_id=request.asset_id)
            if item.field_name in _FIELDS
            and item.frequency is DataFrequency.ANNUAL
            and item.quality is DataQuality.VALID
            and item.source.source_id == self._fundamental_source_id
            and item.available_at <= request.known_at
            and item.period_end is not None
            and item.period_end.date() <= request.valuation_date
        ]
        if not candidates:
            return None
        by_period: dict[datetime, list[NormalizedObservation]] = defaultdict(list)
        for item in candidates:
            if item.period_end is None:
                continue
            by_period[item.period_end].append(item)
        period_end = max(by_period)
        by_revision: dict[tuple[datetime, str, str, str, str], list[NormalizedObservation]] = (
            defaultdict(list)
        )
        metadata_by_revision: dict[tuple[datetime, str, str, str, str], _FilingMetadata] = {}
        for item in by_period[period_end]:
            metadata = _filing_metadata(item)
            key = (
                item.available_at,
                metadata.accession_number,
                metadata.form,
                metadata.fiscal_year,
                metadata.fiscal_period,
            )
            by_revision[key].append(item)
            metadata_by_revision[key] = metadata
        latest_at = max(key[0] for key in by_revision)
        latest_keys = sorted(key for key in by_revision if key[0] == latest_at)
        if len(latest_keys) > 1:
            identities = {_revision_identity(by_revision[key]) for key in latest_keys}
            if len(identities) > 1:
                raise AmbiguousValuationEvidenceError(
                    "equally available fundamental revisions disagree semantically"
                )
        key = latest_keys[0]
        facts: dict[str, NormalizedObservation] = {}
        for item in by_revision[key]:
            current = facts.get(item.field_name)
            if current is not None and _observation_identity(current) != _observation_identity(
                item
            ):
                raise AmbiguousValuationEvidenceError(
                    "one filing contains contradictory fundamental observations"
                )
            if current is None or str(item.observation_id) < str(current.observation_id):
                facts[item.field_name] = item
        duration_starts = {
            item.period_start
            for item in facts.values()
            if item.field_name in _MONETARY_FIELDS and item.period_start is not None
        }
        if len(duration_starts) > 1:
            raise IncompatibleValuationEvidenceError(
                ValuationReasonCode.PERIOD_MISMATCH,
                "annual duration inputs do not share one period_start",
            )
        taxonomies = {_filing_metadata(item).taxonomy for item in facts.values()}
        if len(taxonomies) > 1:
            raise IncompatibleValuationEvidenceError(
                ValuationReasonCode.ACCOUNTING_BASIS_MISMATCH,
                "annual valuation inputs do not share one accounting taxonomy",
            )
        return _AnnualSelection(
            facts=facts,
            metadata=metadata_by_revision[key],
            period_start=next(iter(duration_starts), None),
            period_end=period_end,
            accepted_at=latest_at,
        )

    def _inputs(
        self,
        price: MarketBar,
        selection: _AnnualSelection,
    ) -> tuple[ValuationInput, ...]:
        price_input = ValuationInput(
            role="close_price",
            observation_id=price.observation_ids["close"],
            raw_record_id=price.raw_record_id,
            source_id=price.source_id,
            value=price.close,
            unit=self._price_currency or "USD",
            frequency=price.frequency,
            observed_at=price.timestamp,
            available_at=price.available_at,
        )
        fundamental_inputs = tuple(
            ValuationInput(
                role=field_name.removeprefix("fundamental."),
                observation_id=item.observation_id,
                raw_record_id=item.raw_record_id,
                source_id=item.source.source_id,
                value=item.value,
                unit=item.unit,
                frequency=item.frequency,
                observed_at=item.observed_at,
                period_start=item.period_start,
                period_end=item.period_end,
                available_at=item.available_at,
                accession_number=selection.metadata.accession_number,
                taxonomy=_filing_metadata(item).taxonomy,
                tag=_filing_metadata(item).tag,
            )
            for field_name, item in sorted(selection.facts.items())
        )
        return (price_input, *fundamental_inputs)

    @staticmethod
    def _report_currencies(
        facts: Mapping[str, NormalizedObservation],
    ) -> frozenset[str]:
        return frozenset(item.unit for name, item in facts.items() if name in _MONETARY_FIELDS)

    def _evaluate_metric(
        self,
        definition: ValuationMetricDefinition,
        *,
        request: CorporateValuationRequest,
        price: MarketBar,
        selection: _AnnualSelection,
        security_basis: ValuationSecurityBasis,
        inputs: Mapping[str, ValuationInput],
    ) -> ValuationMetricValue:
        selected_inputs = tuple(inputs[role] for role in definition.input_roles if role in inputs)
        input_ids = tuple(sorted((item.observation_id for item in selected_inputs), key=str))
        available_at = max((item.available_at for item in selected_inputs), default=None)
        missing = tuple(role for role in definition.input_roles if role not in inputs)
        if missing:
            reason = (
                ValuationReasonCode.EBITDA_UNAVAILABLE
                if "depreciation_and_amortization" in missing
                else ValuationReasonCode.MISSING_INPUT
            )
            return ValuationMetricValue(
                metric_key=definition.metric_key,
                status=ValuationStatus.NOT_EVALUABLE,
                reason_code=reason,
                available_at=available_at,
                input_observation_ids=input_ids,
            )
        values = {role: inputs[role].value for role in definition.input_roles}
        value, reason = _calculate(
            definition.metric_key,
            values,
            factor=security_basis.market_units_per_reported_share,
        )
        if reason is not None or value is None:
            return ValuationMetricValue(
                metric_key=definition.metric_key,
                status=ValuationStatus.NOT_EVALUABLE,
                reason_code=reason or ValuationReasonCode.MISSING_INPUT,
                available_at=available_at,
                input_observation_ids=input_ids,
            )
        result_id = valuation_result_id(
            request=request,
            metric_key=definition.metric_key,
            valuation_as_of=price.timestamp.isoformat(),
            annual_period_start=(
                selection.period_start.isoformat() if selection.period_start is not None else None
            ),
            annual_period_end=selection.period_end.isoformat(),
            security_basis_version=security_basis.contract_version,
            input_observation_ids=input_ids,
            algorithm_version=definition.algorithm_version,
        )
        return ValuationMetricValue(
            metric_key=definition.metric_key,
            status=ValuationStatus.EVALUATED,
            value=value,
            result_id=result_id,
            available_at=available_at,
            input_observation_ids=input_ids,
        )


def _filing_metadata(observation: NormalizedObservation) -> _FilingMetadata:
    record_key = observation.source.record_key
    if record_key is None:
        raise MalformedValuationEvidenceError("SEC valuation observation record_key is missing")
    try:
        decoded = json.loads(record_key, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise MalformedValuationEvidenceError(
            "SEC valuation observation record_key is not strict JSON"
        ) from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise MalformedValuationEvidenceError("SEC valuation record_key must be an object")
    return _FilingMetadata(
        accession_number=_required_text(decoded, "accession_number"),
        taxonomy=_required_text(decoded, "taxonomy"),
        tag=_required_text(decoded, "tag"),
        form=_required_text(decoded, "form"),
        fiscal_year=_required_fiscal_year(decoded),
        fiscal_period=_required_text(decoded, "fiscal_period"),
    )


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MalformedValuationEvidenceError(f"SEC valuation record_key lacks {key}")
    return value.strip()


def _required_fiscal_year(mapping: Mapping[str, object]) -> str:
    value = mapping.get("fiscal_year")
    if isinstance(value, bool):
        raise MalformedValuationEvidenceError("SEC valuation record_key lacks fiscal_year")
    if isinstance(value, int) and 1900 <= value <= 10000:
        return str(value)
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    raise MalformedValuationEvidenceError("SEC valuation record_key lacks fiscal_year")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _observation_identity(item: NormalizedObservation) -> tuple[object, ...]:
    return (
        item.field_name,
        item.value,
        item.unit,
        item.frequency,
        item.period_start,
        item.period_end,
        item.source.source_id,
        _filing_metadata(item),
    )


def _revision_identity(items: list[NormalizedObservation]) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted((_observation_identity(item) for item in items), key=str))


def _market_cap(values: Mapping[str, Decimal], factor: Decimal) -> Decimal | None:
    price = values.get("close_price")
    shares = values.get("shares_outstanding")
    if price is None or shares is None:
        return None
    return price * shares * factor


def _debt(values: Mapping[str, Decimal]) -> Decimal | None:
    roles = ("commercial_paper", "long_term_debt_current", "long_term_debt_noncurrent")
    if any(role not in values for role in roles):
        return None
    return sum((values[role] for role in roles), Decimal(0))


def _enterprise_value(values: Mapping[str, Decimal], factor: Decimal) -> Decimal | None:
    market_cap = _market_cap(values, factor)
    debt = _debt(values)
    cash = values.get("cash_and_cash_equivalents")
    if market_cap is None or debt is None or cash is None:
        return None
    return market_cap + debt - cash


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(Context(prec=34)):
        return numerator / denominator


def _calculate(
    key: str,
    values: Mapping[str, Decimal],
    *,
    factor: Decimal,
) -> tuple[Decimal | None, ValuationReasonCode | None]:
    price = values.get("close_price")
    shares = values.get("shares_outstanding")
    if (price is not None and price <= 0) or (shares is not None and shares <= 0):
        return None, ValuationReasonCode.INVALID_DENOMINATOR
    market_cap = _market_cap(values, factor)
    if market_cap is not None and market_cap <= 0:
        return None, ValuationReasonCode.INVALID_DENOMINATOR
    if key.endswith("market_cap"):
        return market_cap, None
    debt = _debt(values)
    if key.endswith("financial_debt"):
        return debt, None
    enterprise_value = _enterprise_value(values, factor)
    if key.endswith("enterprise_value"):
        return enterprise_value, None
    if market_cap is None or market_cap <= 0:
        return None, ValuationReasonCode.INVALID_DENOMINATOR
    if key.endswith("price_to_earnings_latest_annual"):
        denominator = values["net_income"]
        return (
            (None, ValuationReasonCode.INVALID_DENOMINATOR)
            if denominator <= 0
            else (_ratio(market_cap, denominator), None)
        )
    if key.endswith("price_to_book"):
        denominator = values["stockholders_equity"]
        return (
            (None, ValuationReasonCode.INVALID_DENOMINATOR)
            if denominator <= 0
            else (_ratio(market_cap, denominator), None)
        )
    if key.endswith("price_to_sales_latest_annual"):
        denominator = values["revenue"]
        return (
            (None, ValuationReasonCode.INVALID_DENOMINATOR)
            if denominator <= 0
            else (_ratio(market_cap, denominator), None)
        )
    if key.endswith("enterprise_value_to_sales_latest_annual"):
        denominator = values["revenue"]
        return (
            (None, ValuationReasonCode.INVALID_DENOMINATOR)
            if denominator <= 0 or enterprise_value is None
            else (_ratio(enterprise_value, denominator), None)
        )
    if key.endswith("enterprise_value_to_ebit_latest_annual"):
        denominator = values["operating_income"]
        return (
            (None, ValuationReasonCode.INVALID_DENOMINATOR)
            if denominator <= 0 or enterprise_value is None
            else (_ratio(enterprise_value, denominator), None)
        )
    if key.endswith("enterprise_value_to_ebitda_latest_annual"):
        denominator = values["operating_income"] + values["depreciation_and_amortization"]
        return (
            (None, ValuationReasonCode.INVALID_DENOMINATOR)
            if denominator <= 0 or enterprise_value is None
            else (_ratio(enterprise_value, denominator), None)
        )
    if key.endswith("free_cash_flow_yield_latest_annual"):
        free_cash_flow = values["operating_cash_flow"] - values["capital_expenditures"]
        return _ratio(free_cash_flow, market_cap), None
    if key.endswith("earnings_yield_latest_annual"):
        return _ratio(values["net_income"], market_cap), None
    raise CorporateValuationError(f"unsupported valuation metric: {key}")


def _coverage(metrics: tuple[ValuationMetricValue, ...]) -> ValuationCoverage:
    return ValuationCoverage(
        total=len(metrics),
        evaluated=sum(item.status is ValuationStatus.EVALUATED for item in metrics),
        not_evaluable=sum(item.status is ValuationStatus.NOT_EVALUABLE for item in metrics),
        not_applicable=sum(item.status is ValuationStatus.NOT_APPLICABLE for item in metrics),
    )


def _snapshot_status(coverage: ValuationCoverage) -> ValuationSnapshotStatus:
    if coverage.evaluated == coverage.total:
        return ValuationSnapshotStatus.EVALUATED
    if coverage.evaluated > 0:
        return ValuationSnapshotStatus.PARTIAL
    if coverage.not_applicable == coverage.total:
        return ValuationSnapshotStatus.NOT_APPLICABLE
    return ValuationSnapshotStatus.NOT_EVALUABLE


__all__ = [
    "AmbiguousValuationEvidenceError",
    "CorporateValuationError",
    "CorporateValuationService",
    "IncompatibleValuationEvidenceError",
    "MalformedValuationEvidenceError",
]
