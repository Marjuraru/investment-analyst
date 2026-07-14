"""Deterministic Decimal engine for Apple SEC fundamental metrics."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricCandidate,
    SecFundamentalMetricComputation,
    SecFundamentalMetricInput,
    SecFundamentalMetricRequest,
    get_sec_fundamental_metric_definition,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPeriodView,
    SecFundamentalPointInTimeResult,
    SecSelectedFundamentalFact,
)

_SKIP_KEYS = (
    "missing_current_input",
    "missing_previous_period",
    "missing_previous_input",
    "missing_fiscal_metadata",
    "inconsistent_fiscal_metadata",
    "non_positive_revenue",
    "non_positive_assets",
    "non_positive_equity",
    "non_positive_previous_revenue",
    "zero_previous_net_income",
)


class SecFundamentalMetricError(RuntimeError):
    """Base error for deterministic SEC fundamental metric calculations."""


class MalformedSecFundamentalPeriodError(SecFundamentalMetricError):
    """Raised when a point-in-time period contains invalid fundamental facts."""


class AmbiguousSecFundamentalComparisonError(SecFundamentalMetricError):
    """Raised when fiscal metadata maps more than one period to the same comparison key."""


class SecFundamentalMetricComputationError(SecFundamentalMetricError):
    """Raised when a computed metric cannot satisfy its analytical contract."""


@dataclass(frozen=True)
class _FiscalMetadata:
    fiscal_year: int
    fiscal_period: str


@dataclass(frozen=True)
class _PeriodContext:
    period: SecFundamentalPeriodView
    facts: dict[str, SecSelectedFundamentalFact]
    fiscal_metadata: _FiscalMetadata | None
    metadata_status: str


class SecFundamentalMetricEngine:
    """Compute five explicit point-in-time metrics from selected SEC period views."""

    def compute(
        self,
        request: SecFundamentalMetricRequest,
        point_in_time_result: SecFundamentalPointInTimeResult,
        *,
        computed_at: datetime,
    ) -> SecFundamentalMetricComputation:
        """Compute all valid metrics for the requested target periods."""
        computed_at = _utc_datetime(computed_at, "computed_at")
        contexts = self._validate_source(request, point_in_time_result)
        target_contexts = _target_contexts(contexts, request)
        fiscal_index = _build_fiscal_index(contexts)
        candidates: list[SecFundamentalMetricCandidate] = []
        skipped = Counter({key: 0 for key in _SKIP_KEYS})

        with localcontext(Context(prec=34)):
            for context in target_contexts:
                candidates.extend(_same_period_candidates(context, skipped))
                candidates.extend(_year_over_year_candidates(context, fiscal_index, skipped))

        candidates.sort(key=lambda item: (item.period_end, item.metric_name))
        if any(candidate.available_at > computed_at for candidate in candidates):
            raise SecFundamentalMetricComputationError(
                "computed_at must not precede any metric input availability"
            )
        metric_counts = Counter(candidate.metric_name for candidate in candidates)
        return SecFundamentalMetricComputation(
            request=request,
            source_result=point_in_time_result,
            candidates=tuple(candidates),
            metric_counts=dict(sorted(metric_counts.items())),
            skipped_counts=dict(sorted(skipped.items())),
            target_periods=tuple(context.period.period_end for context in target_contexts),
            traceability_verified=True,
        )

    @staticmethod
    def _validate_source(
        request: SecFundamentalMetricRequest,
        result: SecFundamentalPointInTimeResult,
    ) -> list[_PeriodContext]:
        if not result.traceability_verified:
            raise SecFundamentalMetricComputationError(
                "point-in-time source result lacks verified traceability"
            )
        if result.query.asset_id != request.asset_id or request.asset_id != ASSET_ID:
            raise SecFundamentalMetricComputationError("point-in-time result uses another asset")
        if result.query.known_at != request.known_at:
            raise SecFundamentalMetricComputationError("point-in-time known_at does not match")
        if result.query.frequency is not request.frequency:
            raise SecFundamentalMetricComputationError("point-in-time frequency does not match")
        if any(
            value is not None
            for value in (
                result.query.start_period_end,
                result.query.end_period_end,
                result.query.limit,
            )
        ):
            raise SecFundamentalMetricComputationError(
                "point-in-time source must contain the complete available history"
            )

        contexts: list[_PeriodContext] = []
        for period in result.periods:
            contexts.append(_validate_period(period, request))
        period_ends = tuple(context.period.period_end for context in contexts)
        if period_ends != tuple(sorted(period_ends)):
            raise SecFundamentalMetricComputationError("source periods are not chronological")
        return contexts


def _validate_period(
    period: SecFundamentalPeriodView,
    request: SecFundamentalMetricRequest,
) -> _PeriodContext:
    if period.frequency is not request.frequency:
        raise MalformedSecFundamentalPeriodError("period mixes fundamental frequencies")
    if period.period_end.tzinfo is not UTC or period.latest_available_at.tzinfo is not UTC:
        raise MalformedSecFundamentalPeriodError("period timestamps must be normalized to UTC")
    facts: dict[str, SecSelectedFundamentalFact] = {}
    metadata_values: set[_FiscalMetadata] = set()
    metadata_missing = False
    metadata_inconsistent = False

    for fact in period.facts:
        if fact.field_name in facts:
            raise MalformedSecFundamentalPeriodError("period repeats a fundamental field")
        if fact.frequency is not request.frequency or fact.period_end != period.period_end:
            raise MalformedSecFundamentalPeriodError("fact does not belong to its period")
        if fact.source_id != COMPANYFACTS_SOURCE_ID:
            raise MalformedSecFundamentalPeriodError("fact uses another source")
        if fact.unit != "USD":
            raise MalformedSecFundamentalPeriodError("fact must be denominated in USD")
        if fact.available_at > request.known_at:
            raise MalformedSecFundamentalPeriodError("fact was unavailable at known_at")
        for timestamp in (
            fact.period_start,
            fact.period_end,
            fact.available_at,
            fact.normalized_at,
        ):
            if timestamp is not None and timestamp.tzinfo is not UTC:
                raise MalformedSecFundamentalPeriodError(
                    "fact timestamps must be normalized to UTC"
                )
        metadata, status = _fiscal_metadata(fact)
        if status == "missing":
            metadata_missing = True
        elif status == "inconsistent":
            metadata_inconsistent = True
        elif metadata is not None:
            metadata_values.add(metadata)
        facts[fact.field_name] = fact

    if metadata_inconsistent or len(metadata_values) > 1:
        metadata_status = "inconsistent"
        fiscal_metadata = None
    elif metadata_missing or not metadata_values:
        metadata_status = "missing"
        fiscal_metadata = None
    else:
        metadata_status = "valid"
        fiscal_metadata = next(iter(metadata_values))
    return _PeriodContext(period, facts, fiscal_metadata, metadata_status)


def _fiscal_metadata(
    fact: SecSelectedFundamentalFact,
) -> tuple[_FiscalMetadata | None, str]:
    values = (fact.form, fact.fiscal_year, fact.fiscal_period)
    if all(value is None for value in values):
        return None, "missing"
    if any(value is None for value in values):
        return None, "missing"
    assert fact.form is not None
    assert fact.fiscal_year is not None
    assert fact.fiscal_period is not None
    fiscal_year_text = fact.fiscal_year.strip()
    if not fiscal_year_text.isdecimal():
        return None, "inconsistent"
    fiscal_year = int(fiscal_year_text)
    if not 1900 <= fiscal_year <= 10000:
        return None, "inconsistent"
    if fact.frequency is DataFrequency.ANNUAL:
        if fact.form not in {"10-K", "10-K/A"} or fact.fiscal_period != "FY":
            return None, "inconsistent"
    elif fact.frequency is DataFrequency.QUARTERLY:
        if fact.form not in {"10-Q", "10-Q/A"}:
            return None, "inconsistent"
        if fact.fiscal_period not in {"Q1", "Q2", "Q3"}:
            return None, "inconsistent"
    else:
        return None, "inconsistent"
    return _FiscalMetadata(fiscal_year, fact.fiscal_period), "valid"


def _target_contexts(
    contexts: list[_PeriodContext],
    request: SecFundamentalMetricRequest,
) -> list[_PeriodContext]:
    selected = [
        context
        for context in contexts
        if (
            request.start_period_end is None
            or context.period.period_end.date() >= request.start_period_end
        )
        and (
            request.end_period_end is None
            or context.period.period_end.date() <= request.end_period_end
        )
    ]
    if request.limit is not None:
        selected = selected[-request.limit :]
    return selected


def _build_fiscal_index(
    contexts: list[_PeriodContext],
) -> dict[tuple[DataFrequency, int, str], _PeriodContext]:
    index: dict[tuple[DataFrequency, int, str], _PeriodContext] = {}
    for context in contexts:
        metadata = context.fiscal_metadata
        if metadata is None:
            continue
        key = (context.period.frequency, metadata.fiscal_year, metadata.fiscal_period)
        existing = index.get(key)
        if existing is not None and existing.period.period_end != context.period.period_end:
            raise AmbiguousSecFundamentalComparisonError(
                "multiple periods share the same fiscal year and fiscal period"
            )
        index[key] = context
    return index


def _same_period_candidates(
    context: _PeriodContext,
    skipped: Counter[str],
) -> list[SecFundamentalMetricCandidate]:
    candidates: list[SecFundamentalMetricCandidate] = []
    facts = context.facts
    net_income = facts.get("fundamental.net_income")
    revenue = facts.get("fundamental.revenue")
    if net_income is None or revenue is None:
        skipped["missing_current_input"] += 1
    elif revenue.value <= 0:
        skipped["non_positive_revenue"] += 1
    else:
        candidates.append(
            _candidate(
                "fundamental.net_margin",
                context,
                net_income.value / revenue.value,
                (
                    ("current_net_income", net_income),
                    ("current_revenue", revenue),
                ),
            )
        )

    liabilities = facts.get("fundamental.liabilities")
    assets = facts.get("fundamental.assets")
    if liabilities is None or assets is None:
        skipped["missing_current_input"] += 1
    elif assets.value <= 0:
        skipped["non_positive_assets"] += 1
    else:
        candidates.append(
            _candidate(
                "fundamental.liabilities_to_assets",
                context,
                liabilities.value / assets.value,
                (
                    ("current_assets", assets),
                    ("current_liabilities", liabilities),
                ),
            )
        )

    equity = facts.get("fundamental.stockholders_equity")
    if liabilities is None or equity is None:
        skipped["missing_current_input"] += 1
    elif equity.value <= 0:
        skipped["non_positive_equity"] += 1
    else:
        candidates.append(
            _candidate(
                "fundamental.liabilities_to_equity",
                context,
                liabilities.value / equity.value,
                (
                    ("current_equity", equity),
                    ("current_liabilities", liabilities),
                ),
            )
        )
    return candidates


def _year_over_year_candidates(
    context: _PeriodContext,
    fiscal_index: dict[tuple[DataFrequency, int, str], _PeriodContext],
    skipped: Counter[str],
) -> list[SecFundamentalMetricCandidate]:
    candidates: list[SecFundamentalMetricCandidate] = []
    if context.metadata_status == "missing":
        skipped["missing_fiscal_metadata"] += 2
        return candidates
    if context.metadata_status == "inconsistent" or context.fiscal_metadata is None:
        skipped["inconsistent_fiscal_metadata"] += 2
        return candidates

    metadata = context.fiscal_metadata
    previous = fiscal_index.get(
        (context.period.frequency, metadata.fiscal_year - 1, metadata.fiscal_period)
    )
    current_revenue = context.facts.get("fundamental.revenue")
    if current_revenue is None:
        skipped["missing_current_input"] += 1
    elif previous is None:
        skipped["missing_previous_period"] += 1
    else:
        previous_revenue = previous.facts.get("fundamental.revenue")
        if previous_revenue is None:
            skipped["missing_previous_input"] += 1
        elif previous_revenue.value <= 0:
            skipped["non_positive_previous_revenue"] += 1
        else:
            candidates.append(
                _candidate(
                    "fundamental.revenue_yoy_growth",
                    context,
                    current_revenue.value / previous_revenue.value - Decimal("1"),
                    (
                        ("current_revenue", current_revenue),
                        ("previous_revenue", previous_revenue),
                    ),
                )
            )

    current_net_income = context.facts.get("fundamental.net_income")
    if current_net_income is None:
        skipped["missing_current_input"] += 1
    elif previous is None:
        skipped["missing_previous_period"] += 1
    else:
        previous_net_income = previous.facts.get("fundamental.net_income")
        if previous_net_income is None:
            skipped["missing_previous_input"] += 1
        elif previous_net_income.value == 0:
            skipped["zero_previous_net_income"] += 1
        else:
            candidates.append(
                _candidate(
                    "fundamental.net_income_yoy_change_rate",
                    context,
                    (current_net_income.value - previous_net_income.value)
                    / abs(previous_net_income.value),
                    (
                        ("current_net_income", current_net_income),
                        ("previous_net_income", previous_net_income),
                    ),
                )
            )
    return candidates


def _candidate(
    metric_name: str,
    context: _PeriodContext,
    value: Decimal,
    inputs: tuple[tuple[str, SecSelectedFundamentalFact], ...],
) -> SecFundamentalMetricCandidate:
    if not value.is_finite():
        raise SecFundamentalMetricComputationError("computed metric value must be finite")
    definition = get_sec_fundamental_metric_definition(metric_name)
    ordered = tuple(sorted(inputs, key=lambda item: item[0]))
    metadata = context.fiscal_metadata
    return SecFundamentalMetricCandidate(
        asset_id=ASSET_ID,
        metric_name=metric_name,
        value=value,
        unit="ratio",
        frequency=context.period.frequency,
        period_end=context.period.period_end,
        available_at=max(fact.available_at for _, fact in ordered),
        input_roles=tuple(
            SecFundamentalMetricInput(role=role, observation_id=fact.observation_id)
            for role, fact in ordered
        ),
        formula=definition.formula,
        algorithm_version=definition.algorithm_version,
        comparison=definition.comparison,
        fiscal_year=str(metadata.fiscal_year) if metadata is not None else None,
        fiscal_period=metadata.fiscal_period if metadata is not None else None,
        quality=DataQuality.VALID,
    )


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecFundamentalMetricComputationError(
            f"{field_name} must include timezone information"
        )
    return value.astimezone(UTC)
