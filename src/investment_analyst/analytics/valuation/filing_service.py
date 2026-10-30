"""Read-only valuation for one explicitly selected annual filing context."""

from collections import defaultdict
from collections.abc import Collection, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.history_service import AmbiguousRevisionError
from investment_analyst.analytics.valuation.filing_models import (
    CorporateValuationFilingOption,
    CorporateValuationFilingOptions,
    CorporateValuationFilingQuery,
    CorporateValuationFilingSelector,
    CorporateValuationSelectedFilingRequest,
    CorporateValuationSelectedFilingSnapshot,
)
from investment_analyst.analytics.valuation.identity import filing_valuation_result_id_v2
from investment_analyst.analytics.valuation.models import (
    ValuationInput,
    ValuationMetricDefinition,
    ValuationMetricValue,
    ValuationReasonCode,
    ValuationSecurityBasis,
    ValuationSnapshotStatus,
    ValuationStatus,
)
from investment_analyst.analytics.valuation.service import (
    VALUATION_FIELDS,
    VALUATION_IEX_LIMITATION,
    VALUATION_SHARE_LIMITATION,
    AmbiguousValuationEvidenceError,
    AnnualFilingSelection,
    CorporateValuationError,
    IncompatibleValuationEvidenceError,
    build_annual_filing_selection,
    build_valuation_inputs,
    calculate_valuation_value,
    filing_metadata,
    normalize_valuation_computed_at,
    resolve_valuation_price,
    resolve_valuation_security_basis,
    validate_selected_price,
    valuation_coverage,
    valuation_eligibility_reason,
    valuation_metric_definitions,
    valuation_report_currencies,
    valuation_snapshot_status,
)
from investment_analyst.application.analysis_capabilities import AssetAnalysisCapabilities
from investment_analyst.catalog.models import (
    SecurityUnitBasisAmbiguityError,
    SecurityUnitBasisRevision,
)
from investment_analyst.core.models import DataFrequency, DataQuality, NormalizedObservation
from investment_analyst.storage.local import LocalStorage

_FILING_ALGORITHM = "corporate-valuation-selected-annual-filing-v1-decimal34"
_FILING_DEFINITION_VERSION = "corporate-valuation-selected-annual-filing-definition-v1"
_FILING_LIMITATION = (
    "Ejercicio anual elegido explícitamente; no es el último anual disponible, TTM, "
    "forward, consenso ni recomendación."
)

_FILING_DEFINITIONS = valuation_metric_definitions(
    algorithm_version=_FILING_ALGORITHM,
    definition_version=_FILING_DEFINITION_VERSION,
    basis_limitation=_FILING_LIMITATION,
)


class _ObservationRepository(Protocol):
    def list(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        field_names: Collection[str] | None = None,
        frequency: DataFrequency | None = None,
        quality: DataQuality | None = None,
        available_to: datetime | None = None,
        **filters: object,
    ) -> list[NormalizedObservation]: ...


class _Storage(Protocol):
    observations: _ObservationRepository

    def require_open(self) -> None: ...


class CorporateValuationFilingService:
    """Expose eligible annual contexts and value one literal selection."""

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
        security_unit_basis_revisions: tuple[SecurityUnitBasisRevision, ...] = (),
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
        self._security_unit_basis_revisions = security_unit_basis_revisions
        self._semantic_identity_v2 = (
            isinstance(storage, LocalStorage) and storage.paths.format_version == 2
        )

    def query_options(
        self,
        query: CorporateValuationFilingQuery,
    ) -> CorporateValuationFilingOptions:
        """Return one stable page of persisted annual contexts, without metrics."""
        self._storage.require_open()
        if query.asset_id != self._capabilities.asset_id:
            raise CorporateValuationError("filing query asset_id does not match capabilities")
        unavailable = valuation_eligibility_reason(
            capabilities=self._capabilities,
            market_source_id=self._market_source_id,
            fundamental_source_id=self._fundamental_source_id,
        )
        options = () if unavailable is not None else tuple(self._annual_context_options(query))
        page = options[query.offset : query.offset + query.limit]
        limitations: tuple[str, ...] = (_FILING_LIMITATION,)
        if unavailable is not None:
            limitations = (
                "El activo no declara los prerrequisitos de valoración corporativa; "
                "no hay contextos anuales.",
            )
        return CorporateValuationFilingOptions(
            request=query,
            total=len(options),
            offset=query.offset,
            limit=query.limit,
            returned=len(page),
            options=page,
            limitations=limitations,
        )

    def query(
        self,
        request: CorporateValuationSelectedFilingRequest,
        *,
        computed_at: datetime | None = None,
        selected_price: MarketBar | None = None,
    ) -> CorporateValuationSelectedFilingSnapshot:
        """Reconstruct one selected filing using only evidence eligible at the cut."""
        self._storage.require_open()
        now = normalize_valuation_computed_at(computed_at)
        if request.asset_id != self._capabilities.asset_id:
            raise CorporateValuationError("valuation request asset_id does not match capabilities")
        unavailable = valuation_eligibility_reason(
            capabilities=self._capabilities,
            market_source_id=self._market_source_id,
            fundamental_source_id=self._fundamental_source_id,
        )
        if unavailable is not None:
            status = (
                ValuationStatus.NOT_APPLICABLE
                if unavailable is ValuationReasonCode.ASSET_NOT_APPLICABLE
                else ValuationStatus.NOT_EVALUABLE
            )
            return self._unavailable(request, now, unavailable, status)
        try:
            security_basis = resolve_valuation_security_basis(
                known_at=request.known_at,
                valuation_date=request.valuation_date,
                security_unit_factor=self._security_unit_factor,
                security_unit_basis=self._security_unit_basis,
                security_unit_basis_version=self._security_unit_basis_version,
                security_unit_market_adjustment=self._security_unit_market_adjustment,
                security_unit_basis_revisions=self._security_unit_basis_revisions,
            )
        except SecurityUnitBasisAmbiguityError:
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.CORPORATE_ACTION_BASIS_UNAVAILABLE,
                ValuationStatus.NOT_EVALUABLE,
            )
        if security_basis is None:
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.SHARE_BASIS_UNAVAILABLE,
                ValuationStatus.NOT_EVALUABLE,
            )
        try:
            price = (
                validate_selected_price(
                    selected_price,
                    asset_id=request.asset_id,
                    market_source_id=self._market_source_id,
                    known_at=request.known_at,
                    valuation_date=request.valuation_date,
                )
                if selected_price is not None
                else resolve_valuation_price(
                    self._storage,
                    asset_id=request.asset_id,
                    market_source_id=self._market_source_id,
                    known_at=request.known_at,
                    valuation_date=request.valuation_date,
                    semantic_identity_v2=self._semantic_identity_v2,
                )
            )
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
        basis_evidence = security_basis.basis_evidence
        if basis_evidence is not None and not basis_evidence.covers(price.timestamp.date()):
            return self._unavailable(
                request,
                now,
                ValuationReasonCode.SECURITY_UNIT_MISMATCH,
                ValuationStatus.NOT_EVALUABLE,
                security_basis=security_basis,
            )
        try:
            selection = self._selected_context(request)
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
        report_currencies = valuation_report_currencies(selection.facts)
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
        inputs = build_valuation_inputs(
            price,
            selection,
            price_currency=self._price_currency,
        )
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
            for definition in _FILING_DEFINITIONS
        )
        available_at = max(item.available_at for item in inputs)
        if basis_evidence is not None:
            available_at = max(available_at, basis_evidence.available_at)
        coverage = valuation_coverage(metrics)
        limitations = [
            _FILING_LIMITATION,
            VALUATION_IEX_LIMITATION,
            VALUATION_SHARE_LIMITATION,
        ]
        if (request.valuation_date - price.timestamp.date()).days > 3:
            limitations.append(
                "El último precio elegible está retrasado respecto de valuation_date."
            )
        return CorporateValuationSelectedFilingSnapshot(
            asset_id=request.asset_id,
            request=request,
            status=valuation_snapshot_status(coverage),
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
            definitions=_FILING_DEFINITIONS,
            metrics=metrics,
            coverage=coverage,
            limitations=tuple(limitations),
        )

    def _annual_context_options(
        self,
        query: CorporateValuationFilingQuery,
    ) -> list[CorporateValuationFilingOption]:
        grouped: dict[
            tuple[datetime, datetime, str, str, str, str],
            list[NormalizedObservation],
        ] = defaultdict(list)
        for item in self._annual_observations(query.known_at):
            if item.period_end is None:
                continue
            if not query.start_date <= item.period_end.date() <= query.end_date:
                continue
            if item.period_end.date() > query.valuation_date:
                continue
            metadata = filing_metadata(item)
            key = (
                item.period_end,
                item.available_at,
                metadata.accession_number,
                metadata.form,
                metadata.fiscal_year,
                metadata.fiscal_period,
            )
            grouped[key].append(item)
        options: list[CorporateValuationFilingOption] = []
        for key, items in grouped.items():
            options.append(
                CorporateValuationFilingOption(
                    selector=CorporateValuationFilingSelector(
                        annual_period_end=key[0].date(),
                        filing_accession_number=key[2],
                        filing_accepted_at=key[1],
                        filing_form=key[3],
                        fiscal_year=key[4],
                        fiscal_period=key[5],
                    ),
                    available_field_roles=tuple(
                        sorted({item.field_name.removeprefix("fundamental.") for item in items})
                    ),
                    taxonomies=tuple(sorted({filing_metadata(item).taxonomy for item in items})),
                )
            )
        options.sort(
            key=lambda option: (
                option.selector.filing_accession_number,
                option.selector.filing_form,
                option.selector.fiscal_year,
                option.selector.fiscal_period,
            )
        )
        options.sort(key=lambda option: option.selector.filing_accepted_at, reverse=True)
        options.sort(key=lambda option: option.selector.annual_period_end, reverse=True)
        return options

    def _selected_context(
        self,
        request: CorporateValuationSelectedFilingRequest,
    ) -> AnnualFilingSelection | None:
        matching: list[NormalizedObservation] = []
        for item in self._annual_observations(request.known_at):
            if item.period_end is None:
                continue
            if item.period_end.date() != request.annual_period_end:
                continue
            if item.period_end.date() > request.valuation_date:
                continue
            if item.available_at != request.filing_accepted_at:
                continue
            metadata = filing_metadata(item)
            if (
                metadata.accession_number != request.filing_accession_number
                or metadata.form != request.filing_form
                or metadata.fiscal_year != request.fiscal_year
                or metadata.fiscal_period != request.fiscal_period
            ):
                continue
            matching.append(item)
        if not matching:
            return None
        metadata = filing_metadata(matching[0])
        return build_annual_filing_selection(
            matching,
            metadata=metadata,
            period_end=matching[0].period_end,
            accepted_at=request.filing_accepted_at,
        )

    def _annual_observations(self, known_at: datetime) -> list[NormalizedObservation]:
        return [
            item
            for item in self._storage.observations.list(
                asset_id=self._capabilities.asset_id,
                source_id=self._fundamental_source_id,
                field_names=VALUATION_FIELDS,
                frequency=DataFrequency.ANNUAL,
                quality=DataQuality.VALID,
                available_to=known_at,
            )
            if item.field_name in VALUATION_FIELDS
            and item.frequency is DataFrequency.ANNUAL
            and item.quality is DataQuality.VALID
            and item.source.source_id == self._fundamental_source_id
            and item.available_at <= known_at
            and item.period_end is not None
        ]

    def _evaluate_metric(
        self,
        definition: ValuationMetricDefinition,
        *,
        request: CorporateValuationSelectedFilingRequest,
        price: MarketBar,
        selection: AnnualFilingSelection,
        security_basis: ValuationSecurityBasis,
        inputs: Mapping[str, ValuationInput],
    ) -> ValuationMetricValue:
        selected_inputs = tuple(inputs[role] for role in definition.input_roles if role in inputs)
        input_ids = tuple(sorted((item.observation_id for item in selected_inputs), key=str))
        available_at = max((item.available_at for item in selected_inputs), default=None)
        basis_evidence = security_basis.basis_evidence
        if basis_evidence is not None:
            available_at = (
                basis_evidence.available_at
                if available_at is None
                else max(available_at, basis_evidence.available_at)
            )
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
        value, reason = calculate_valuation_value(
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
        if available_at is None:
            raise CorporateValuationError("evaluated valuation metric lacks available_at")
        result_id = filing_valuation_result_id_v2(
            request=request,
            metric_key=definition.metric_key,
            valuation_as_of=price.timestamp,
            available_at=available_at,
            unit=definition.unit,
            annual_period_start=selection.period_start,
            annual_period_end=selection.period_end,
            security_basis_version=security_basis.contract_version,
            input_observation_ids=input_ids,
            algorithm_version=definition.algorithm_version,
            formula=definition.formula,
            market_units_per_reported_share=security_basis.market_units_per_reported_share,
            basis_evidence=security_basis.basis_evidence,
        )
        return ValuationMetricValue(
            metric_key=definition.metric_key,
            status=ValuationStatus.EVALUATED,
            value=value,
            result_id=result_id,
            available_at=available_at,
            input_observation_ids=input_ids,
        )

    def _unavailable(
        self,
        request: CorporateValuationSelectedFilingRequest,
        now: datetime,
        reason: ValuationReasonCode,
        status: ValuationStatus,
        *,
        security_basis: ValuationSecurityBasis | None = None,
    ) -> CorporateValuationSelectedFilingSnapshot:
        metrics = tuple(
            ValuationMetricValue(
                metric_key=item.metric_key,
                status=status,
                reason_code=reason,
            )
            for item in _FILING_DEFINITIONS
        )
        return CorporateValuationSelectedFilingSnapshot(
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
            definitions=_FILING_DEFINITIONS,
            metrics=metrics,
            coverage=valuation_coverage(metrics),
            limitations=(_FILING_LIMITATION,),
        )


__all__ = ["CorporateValuationFilingService"]
