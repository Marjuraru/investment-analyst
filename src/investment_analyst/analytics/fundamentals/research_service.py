"""Read-only point-in-time engine for fundamental research metrics."""

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date
from decimal import Context, Decimal, localcontext
from typing import Protocol
from uuid import UUID

from investment_analyst.analytics.fundamentals.research_models import (
    FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS,
    AaplFundamentalResearchCoverage,
    AaplFundamentalResearchPeriod,
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
    FundamentalResearchMetricDefinition,
    FundamentalResearchMetricInput,
    FundamentalResearchMetricValue,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
    get_sec_fact_definition,
)

_RESEARCH_FIELDS = frozenset(
    field.field_name
    for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
    for field in definition.input_fields
)
_REQUIRED_RECORD_KEY_FIELDS = frozenset(
    {
        "accession_number",
        "companyfacts_record_id",
        "period",
        "submissions_record_id",
        "tag",
        "taxonomy",
        "unit",
    }
)


class _ObservationRepository(Protocol):
    def list(self, *, asset_id: str | None = None) -> list[NormalizedObservation]:
        """Return stored observations in repository order."""
        ...


class _ResearchStorage(Protocol):
    observations: _ObservationRepository

    def require_open(self) -> None:
        """Reject operations on a closed storage facade."""
        ...


class FundamentalResearchError(RuntimeError):
    """Base error for point-in-time fundamental research."""


class MalformedFundamentalResearchObservationError(FundamentalResearchError):
    """Raised when apparent SEC evidence has invalid audit metadata."""


class AmbiguousFundamentalResearchRevisionError(FundamentalResearchError):
    """Raised when equally available observations disagree semantically."""


class FundamentalResearchComputationError(FundamentalResearchError):
    """Raised when a published metric cannot be calculated safely."""


class AaplFundamentalResearchService:
    """Select SEC evidence and compute descriptive metrics without writes."""

    def __init__(self, storage: _ResearchStorage) -> None:
        storage.require_open()
        self._storage = storage

    def query(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchResult:
        """Return one bounded, deterministic, point-in-time research result."""
        self._storage.require_open()
        observations = self._storage.observations.list(asset_id=ASSET_ID)
        eligible = [
            observation for observation in observations if _is_eligible(observation, request)
        ]
        selected, superseded = _resolve_revisions(eligible)

        grouped: dict[date, dict[str, NormalizedObservation]] = defaultdict(dict)
        for observation in selected.values():
            if observation.period_end is None:
                raise MalformedFundamentalResearchObservationError(
                    "selected SEC research observation lacks period_end"
                )
            grouped[observation.period_end.date()][observation.field_name] = observation

        period_ends = sorted(grouped)
        if request.limit is not None:
            period_ends = period_ends[-request.limit :]

        metric_counts = Counter(
            {definition.metric_key: 0 for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS}
        )
        skipped: Counter[str] = Counter()
        periods: list[AaplFundamentalResearchPeriod] = []
        selected_count = 0
        superseded_count = 0

        with localcontext(Context(prec=34)):
            for period_end in period_ends:
                facts = grouped[period_end]
                selected_count += len(facts)
                superseded_count += sum(
                    superseded[(field_name, period_end)] for field_name in facts
                )
                metrics: list[FundamentalResearchMetricValue] = []
                for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS:
                    metric, reason = _calculate_metric(
                        definition,
                        facts,
                        request.frequency,
                    )
                    if metric is None:
                        skipped[f"{definition.metric_key}:{reason}"] += 1
                        continue
                    metrics.append(metric)
                    metric_counts[metric.metric_key] += 1
                if metrics:
                    periods.append(
                        AaplFundamentalResearchPeriod(
                            period_end=metrics[0].period_end,
                            frequency=request.frequency,
                            metrics=tuple(sorted(metrics, key=lambda item: item.metric_key)),
                        )
                    )

        return AaplFundamentalResearchResult(
            request=request,
            periods=tuple(periods),
            coverage=AaplFundamentalResearchCoverage(
                observations_examined=len(observations),
                observations_eligible=len(eligible),
                observations_selected=selected_count,
                observations_superseded=superseded_count,
                source_periods=len(period_ends),
                output_periods=len(periods),
                metrics_returned=sum(metric_counts.values()),
                metric_counts=dict(metric_counts),
                skipped_counts=dict(sorted(skipped.items())),
                earliest_period_end=periods[0].period_end if periods else None,
                latest_period_end=periods[-1].period_end if periods else None,
            ),
        )


def _is_eligible(
    observation: NormalizedObservation,
    request: AaplFundamentalResearchRequest,
) -> bool:
    if observation.field_name not in _RESEARCH_FIELDS:
        return False
    if observation.source.source_id != COMPANYFACTS_SOURCE_ID:
        return False
    if observation.transformation_version != TRANSFORMATION_VERSION:
        return False
    if observation.quality is not DataQuality.VALID:
        return False
    if observation.frequency is not request.frequency:
        return False
    if observation.available_at > request.known_at:
        return False
    if observation.period_end is None:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation lacks period_end"
        )
    period_end = observation.period_end.date()
    if request.start_period_end is not None and period_end < request.start_period_end:
        return False
    if request.end_period_end is not None and period_end > request.end_period_end:
        return False
    _validate_observation(observation)
    return True


def _validate_observation(observation: NormalizedObservation) -> None:
    if observation.asset_id != ASSET_ID:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation must belong to Apple"
        )
    definition = get_sec_fact_definition(observation.field_name)
    if observation.unit != definition.unit:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation unit does not match its field definition"
        )
    if not observation.value.is_finite():
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation value must be finite"
        )
    if observation.period_end is None:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation lacks period_end"
        )
    record_key = _record_key(observation)
    if _required_string(record_key, "taxonomy") != definition.taxonomy:
        raise MalformedFundamentalResearchObservationError(
            "SEC research taxonomy does not match its field definition"
        )
    if _required_string(record_key, "tag") != definition.tag:
        raise MalformedFundamentalResearchObservationError(
            "SEC research tag does not match its field definition"
        )
    if _required_string(record_key, "unit") != observation.unit:
        raise MalformedFundamentalResearchObservationError(
            "SEC research record unit does not match its observation"
        )
    if _required_string(record_key, "period") != observation.period_end.date().isoformat():
        raise MalformedFundamentalResearchObservationError(
            "SEC research record period does not match its observation"
        )
    if _required_uuid(record_key, "companyfacts_record_id") != observation.raw_record_id:
        raise MalformedFundamentalResearchObservationError(
            "SEC research RawRecord identity is inconsistent"
        )
    _required_uuid(record_key, "submissions_record_id")
    _required_string(record_key, "accession_number")


def _record_key(observation: NormalizedObservation) -> Mapping[str, object]:
    encoded = observation.source.record_key
    if encoded is None:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation record_key is missing"
        )
    try:
        decoded = json.loads(encoded, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation record_key must be strict JSON"
        ) from error
    if not isinstance(decoded, Mapping) or not all(isinstance(key, str) for key in decoded):
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation record_key must be an object"
        )
    missing = _REQUIRED_RECORD_KEY_FIELDS - set(decoded)
    if missing:
        raise MalformedFundamentalResearchObservationError(
            "SEC research observation record_key is missing required fields: "
            + ", ".join(sorted(missing))
        )
    return decoded


def _resolve_revisions(
    observations: list[NormalizedObservation],
) -> tuple[
    dict[tuple[str, date], NormalizedObservation],
    dict[tuple[str, date], int],
]:
    groups: dict[tuple[str, date], list[NormalizedObservation]] = defaultdict(list)
    for observation in observations:
        if observation.period_end is None:
            raise MalformedFundamentalResearchObservationError(
                "SEC research observation lacks period_end"
            )
        groups[(observation.field_name, observation.period_end.date())].append(observation)

    selected: dict[tuple[str, date], NormalizedObservation] = {}
    superseded: dict[tuple[str, date], int] = {}
    for key, revisions in groups.items():
        latest_available_at = max(item.available_at for item in revisions)
        latest = [item for item in revisions if item.available_at == latest_available_at]
        semantics = {
            (
                item.value,
                item.period_start,
                item.period_end,
                item.source.record_key,
            )
            for item in latest
        }
        if len(semantics) != 1:
            raise AmbiguousFundamentalResearchRevisionError(
                f"equally available SEC research revisions disagree for {key[0]} {key[1]}"
            )
        latest.sort(key=lambda item: str(item.observation_id))
        selected[key] = latest[0]
        superseded[key] = len(revisions) - 1
    return selected, superseded


def _calculate_metric(
    definition: FundamentalResearchMetricDefinition,
    facts: dict[str, NormalizedObservation],
    frequency: DataFrequency,
) -> tuple[FundamentalResearchMetricValue | None, str]:
    selected: list[tuple[str, NormalizedObservation]] = []
    for required in definition.input_fields:
        observation = facts.get(required.field_name)
        if observation is None:
            return None, "missing_inputs"
        selected.append((required.role, observation))

    values = {role: observation.value for role, observation in selected}
    value, reason = _formula(definition.metric_key, values)
    if value is None:
        return None, reason
    if not value.is_finite():
        raise FundamentalResearchComputationError(
            f"research metric {definition.metric_key} produced a non-finite value"
        )
    period_ends = {item.period_end for _, item in selected}
    if len(period_ends) != 1 or None in period_ends:
        raise FundamentalResearchComputationError(
            f"research metric {definition.metric_key} mixes reporting periods"
        )
    period_end = next(iter(period_ends))
    if period_end is None:
        raise FundamentalResearchComputationError("research metric period cannot be null")
    inputs = tuple(
        FundamentalResearchMetricInput(
            role=role,
            field_name=observation.field_name,
            observation_id=observation.observation_id,
            value=observation.value,
            unit=observation.unit,
            available_at=observation.available_at,
        )
        for role, observation in selected
    )
    return (
        FundamentalResearchMetricValue(
            metric_key=definition.metric_key,
            display_name_es=definition.display_name_es,
            value=value,
            unit=definition.unit,
            frequency=frequency,
            period_end=period_end,
            available_at=max(item.available_at for item in inputs),
            formula=definition.formula,
            algorithm_version=definition.algorithm_version,
            inputs=inputs,
            limitations=definition.limitations,
        ),
        "",
    )


def _formula(
    metric_key: str,
    values: dict[str, Decimal],
) -> tuple[Decimal | None, str]:
    if metric_key == "fundamental.research.asset_turnover":
        return _positive_ratio(values["revenue"], values["assets"])
    if metric_key == "fundamental.research.capex_to_operating_cash_flow":
        denominator = values["operating_cash_flow"]
        return _positive_ratio(values["capital_expenditures"], denominator)
    if metric_key == "fundamental.research.cash_ratio":
        return _positive_ratio(values["cash_and_cash_equivalents"], values["current_liabilities"])
    if metric_key == "fundamental.research.current_financial_debt":
        return _current_financial_debt(values), ""
    if metric_key == "fundamental.research.current_financial_debt_share":
        return _positive_ratio(_current_financial_debt(values), _financial_debt(values))
    if metric_key == "fundamental.research.current_ratio":
        return _positive_ratio(values["current_assets"], values["current_liabilities"])
    if metric_key == "fundamental.research.diluted_eps":
        return values["diluted_earnings_per_share"], ""
    if metric_key == "fundamental.research.diluted_shares":
        return values["weighted_average_diluted_shares"], ""
    if metric_key == "fundamental.research.effective_tax_rate":
        return _effective_tax_rate(values)
    if metric_key == "fundamental.research.financial_debt":
        return _financial_debt(values), ""
    if metric_key == "fundamental.research.financial_debt_to_assets":
        return _positive_ratio(_financial_debt(values), values["assets"])
    if metric_key == "fundamental.research.financial_debt_to_equity":
        return _positive_ratio(_financial_debt(values), values["stockholders_equity"])
    if metric_key == "fundamental.research.financial_debt_to_free_cash_flow":
        free_cash_flow = values["operating_cash_flow"] - values["capital_expenditures"]
        return _positive_ratio(_financial_debt(values), free_cash_flow)
    if metric_key == "fundamental.research.fixed_asset_turnover":
        return _positive_ratio(values["revenue"], values["property_plant_and_equipment_net"])
    if metric_key == "fundamental.research.free_cash_flow":
        return values["operating_cash_flow"] - values["capital_expenditures"], ""
    if metric_key == "fundamental.research.free_cash_flow_margin":
        free_cash_flow = values["operating_cash_flow"] - values["capital_expenditures"]
        return _positive_ratio(free_cash_flow, values["revenue"])
    if metric_key == "fundamental.research.free_cash_flow_per_diluted_share":
        free_cash_flow = values["operating_cash_flow"] - values["capital_expenditures"]
        return _positive_ratio(free_cash_flow, values["weighted_average_diluted_shares"])
    if metric_key == "fundamental.research.free_cash_flow_to_net_income":
        free_cash_flow = values["operating_cash_flow"] - values["capital_expenditures"]
        return _positive_ratio(free_cash_flow, values["net_income"])
    if metric_key == "fundamental.research.gross_margin":
        return _positive_ratio(values["gross_profit"], values["revenue"])
    if metric_key == "fundamental.research.interest_coverage":
        return _positive_ratio(values["operating_income"], values["interest_expense"])
    if metric_key == "fundamental.research.lease_liabilities":
        return _lease_liabilities(values), ""
    if metric_key == "fundamental.research.net_debt":
        return _net_debt(values), ""
    if metric_key == "fundamental.research.net_debt_to_free_cash_flow":
        free_cash_flow = values["operating_cash_flow"] - values["capital_expenditures"]
        return _positive_ratio(_net_debt(values), free_cash_flow)
    if metric_key == "fundamental.research.net_liquid_assets":
        return (
            values["cash_and_cash_equivalents"]
            + values["marketable_securities_current"]
            + values["marketable_securities_noncurrent"]
            - values["long_term_debt_current"]
            - values["long_term_debt_noncurrent"],
            "",
        )
    if metric_key == "fundamental.research.net_margin":
        return _positive_ratio(values["net_income"], values["revenue"])
    if metric_key == "fundamental.research.operating_cash_flow_margin":
        return _positive_ratio(values["operating_cash_flow"], values["revenue"])
    if metric_key == "fundamental.research.operating_cash_flow_to_net_income":
        return _positive_ratio(values["operating_cash_flow"], values["net_income"])
    if metric_key == "fundamental.research.operating_margin":
        return _positive_ratio(values["operating_income"], values["revenue"])
    if metric_key == "fundamental.research.research_and_development_to_revenue":
        return _positive_ratio(values["research_and_development"], values["revenue"])
    if metric_key == "fundamental.research.return_on_assets_ending_balance":
        return _positive_ratio(values["net_income"], values["assets"])
    if metric_key == "fundamental.research.return_on_equity_ending_balance":
        return _positive_ratio(values["net_income"], values["stockholders_equity"])
    if metric_key == "fundamental.research.return_on_invested_capital_ending_balance":
        tax_rate, reason = _effective_tax_rate(values)
        if tax_rate is None:
            return None, reason
        nopat = values["operating_income"] * (Decimal(1) - tax_rate)
        invested_capital = (
            values["stockholders_equity"]
            + _financial_debt(values)
            - _liquid_financial_assets(values)
        )
        return _positive_ratio(nopat, invested_capital)
    if metric_key == "fundamental.research.revenue_per_diluted_share":
        return _positive_ratio(values["revenue"], values["weighted_average_diluted_shares"])
    if metric_key == ("fundamental.research.selling_general_and_administrative_to_revenue"):
        return _positive_ratio(values["selling_general_and_administrative"], values["revenue"])
    if metric_key == "fundamental.research.share_based_compensation_to_revenue":
        return _positive_ratio(values["share_based_compensation"], values["revenue"])
    if metric_key == "fundamental.research.shareholder_distributions":
        return values["dividends_paid"] + values["share_repurchases"], ""
    if metric_key == "fundamental.research.shareholder_distributions_to_free_cash_flow":
        numerator = values["dividends_paid"] + values["share_repurchases"]
        denominator = values["operating_cash_flow"] - values["capital_expenditures"]
        return _positive_ratio(numerator, denominator)
    if metric_key == "fundamental.research.shares_outstanding":
        return values["shares_outstanding"], ""
    if metric_key == "fundamental.research.total_financial_obligations":
        return _financial_debt(values) + _lease_liabilities(values), ""
    if metric_key == "fundamental.research.working_capital":
        return values["current_assets"] - values["current_liabilities"], ""
    raise FundamentalResearchComputationError(
        f"unsupported fundamental research formula: {metric_key}"
    )


def _current_financial_debt(values: dict[str, Decimal]) -> Decimal:
    return values["commercial_paper"] + values["long_term_debt_current"]


def _financial_debt(values: dict[str, Decimal]) -> Decimal:
    return _current_financial_debt(values) + values["long_term_debt_noncurrent"]


def _liquid_financial_assets(values: dict[str, Decimal]) -> Decimal:
    return (
        values["cash_and_cash_equivalents"]
        + values["marketable_securities_current"]
        + values["marketable_securities_noncurrent"]
    )


def _lease_liabilities(values: dict[str, Decimal]) -> Decimal:
    return (
        values["finance_lease_liability_current"]
        + values["finance_lease_liability_noncurrent"]
        + values["operating_lease_liability_current"]
        + values["operating_lease_liability_noncurrent"]
    )


def _net_debt(values: dict[str, Decimal]) -> Decimal:
    return _financial_debt(values) - _liquid_financial_assets(values)


def _effective_tax_rate(values: dict[str, Decimal]) -> tuple[Decimal | None, str]:
    rate, reason = _positive_ratio(values["income_tax_expense"], values["income_before_tax"])
    if rate is None:
        return None, reason
    if rate < 0 or rate > 1:
        return None, "tax_rate_out_of_range"
    return rate, ""


def _positive_ratio(numerator: Decimal, denominator: Decimal) -> tuple[Decimal | None, str]:
    if denominator <= 0:
        return None, "non_positive_denominator"
    return numerator / denominator, ""


def _required_string(mapping: Mapping[str, object], field_name: str) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedFundamentalResearchObservationError(
            f"SEC research record field {field_name} must be a non-empty string"
        )
    return value.strip()


def _required_uuid(mapping: Mapping[str, object], field_name: str) -> UUID:
    try:
        return UUID(_required_string(mapping, field_name))
    except ValueError as error:
        raise MalformedFundamentalResearchObservationError(
            f"SEC research record field {field_name} must be a UUID"
        ) from error


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


__all__ = [
    "AmbiguousFundamentalResearchRevisionError",
    "AaplFundamentalResearchService",
    "FundamentalResearchComputationError",
    "FundamentalResearchError",
    "MalformedFundamentalResearchObservationError",
]
