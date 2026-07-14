"""Published Decimal-only rules for Apple fundamental diagnostics."""

from datetime import datetime
from decimal import Context, Decimal, localcontext

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticVerdict,
    EvidenceDirection,
)

ALGORITHM_VERSION = "sec-aapl-fundamental-diagnostic-v1.1-decimal34"
SCORE_TOLERANCE = Decimal("0.0001")
POSITIVE_THRESHOLD = Decimal("65")
NEUTRAL_THRESHOLD = Decimal("40")
MINIMUM_COVERAGE = Decimal("0.60")

BASE_WEIGHTS = {
    "fundamental.net_margin": Decimal("0.30"),
    "fundamental.liabilities_to_assets": Decimal("0.25"),
    "fundamental.liabilities_to_equity": Decimal("0.15"),
    "fundamental.revenue_yoy_growth": Decimal("0.15"),
    "fundamental.net_income_yoy_change_rate": Decimal("0.15"),
}
REQUIRED_SCORE_METRICS = (
    "fundamental.net_margin",
    "fundamental.liabilities_to_assets",
)


class SecFundamentalDiagnosticRuleError(RuntimeError):
    """Raised when published scoring or confidence rules receive invalid inputs."""


def _decimal(value: Decimal, name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    """Clamp a finite Decimal to an inclusive Decimal interval."""
    value = _decimal(value, "value")
    minimum = _decimal(minimum, "minimum")
    maximum = _decimal(maximum, "maximum")
    if minimum > maximum:
        raise ValueError("minimum must not exceed maximum")
    return min(max(value, minimum), maximum)


def score_net_margin(value: Decimal) -> Decimal:
    """Score net margin from zero at 0.00 to one hundred at 0.25."""
    value = _decimal(value, "net margin")
    with localcontext(Context(prec=34)):
        if value <= Decimal("0.00"):
            return Decimal("0")
        if value >= Decimal("0.25"):
            return Decimal("100")
        return value / Decimal("0.25") * Decimal("100")


def score_liabilities_to_assets(value: Decimal) -> Decimal:
    """Score liabilities to assets using the published lower-is-better interval."""
    value = _decimal(value, "liabilities to assets")
    if value < 0:
        raise ValueError("liabilities to assets must be non-negative")
    with localcontext(Context(prec=34)):
        if value <= Decimal("0.40"):
            return Decimal("100")
        if value >= Decimal("0.90"):
            return Decimal("0")
        return (Decimal("0.90") - value) / Decimal("0.50") * Decimal("100")


def score_liabilities_to_equity(value: Decimal) -> Decimal:
    """Score liabilities to equity using the published lower-is-better interval."""
    value = _decimal(value, "liabilities to equity")
    if value < 0:
        raise ValueError("liabilities to equity must be non-negative")
    with localcontext(Context(prec=34)):
        if value <= Decimal("0.50"):
            return Decimal("100")
        if value >= Decimal("5.00"):
            return Decimal("0")
        return (Decimal("5.00") - value) / Decimal("4.50") * Decimal("100")


def score_revenue_yoy_growth(value: Decimal) -> Decimal:
    """Score revenue year-over-year growth over the published interval."""
    value = _decimal(value, "revenue year-over-year growth")
    with localcontext(Context(prec=34)):
        if value <= Decimal("-0.10"):
            return Decimal("0")
        if value >= Decimal("0.15"):
            return Decimal("100")
        return (value + Decimal("0.10")) / Decimal("0.25") * Decimal("100")


def score_net_income_yoy_change_rate(value: Decimal) -> Decimal:
    """Score net-income relative change over the published symmetric interval."""
    value = _decimal(value, "net income year-over-year change rate")
    with localcontext(Context(prec=34)):
        if value <= Decimal("-0.50"):
            return Decimal("0")
        if value >= Decimal("0.50"):
            return Decimal("100")
        return (value + Decimal("0.50")) * Decimal("100")


_SCORE_FUNCTIONS = {
    "fundamental.net_margin": score_net_margin,
    "fundamental.liabilities_to_assets": score_liabilities_to_assets,
    "fundamental.liabilities_to_equity": score_liabilities_to_equity,
    "fundamental.revenue_yoy_growth": score_revenue_yoy_growth,
    "fundamental.net_income_yoy_change_rate": score_net_income_yoy_change_rate,
}


def score_metric(metric_name: str, value: Decimal) -> Decimal:
    """Apply the exact published score rule for one supported metric."""
    try:
        scorer = _SCORE_FUNCTIONS[metric_name]
    except KeyError as error:
        raise SecFundamentalDiagnosticRuleError(
            f"unsupported fundamental diagnostic metric: {metric_name}"
        ) from error
    score = scorer(value)
    return clamp(score, Decimal("0"), Decimal("100"))


def coverage_for(metric_names: tuple[str, ...]) -> Decimal:
    """Return the sum of base weights represented by available metrics."""
    if len(set(metric_names)) != len(metric_names):
        raise SecFundamentalDiagnosticRuleError("metric names must be unique")
    try:
        return sum((BASE_WEIGHTS[name] for name in metric_names), Decimal("0"))
    except KeyError as error:
        raise SecFundamentalDiagnosticRuleError(
            f"unsupported metric in coverage: {error.args[0]}"
        ) from error


def normalized_weights(metric_names: tuple[str, ...]) -> dict[str, Decimal]:
    """Renormalize available base weights to sum to one."""
    coverage = coverage_for(metric_names)
    if coverage <= 0:
        raise SecFundamentalDiagnosticRuleError("coverage must be positive")
    with localcontext(Context(prec=34)):
        weights = {name: BASE_WEIGHTS[name] / coverage for name in metric_names}
    total = sum(weights.values(), Decimal("0"))
    if abs(total - Decimal("1")) > SCORE_TOLERANCE:
        raise SecFundamentalDiagnosticRuleError("normalized weights do not sum to one")
    return weights


def has_minimum_coverage(metric_names: tuple[str, ...]) -> bool:
    """Return whether mandatory metrics and minimum base coverage are present."""
    names = set(metric_names)
    return (
        all(name in names for name in REQUIRED_SCORE_METRICS)
        and coverage_for(metric_names) >= MINIMUM_COVERAGE
    )


def verdict_for(score: Decimal) -> DiagnosticVerdict:
    """Map a final Decimal score to the published descriptive verdict."""
    score = _decimal(score, "final score")
    if score >= POSITIVE_THRESHOLD:
        return DiagnosticVerdict.POSITIVE
    if score >= NEUTRAL_THRESHOLD:
        return DiagnosticVerdict.NEUTRAL
    return DiagnosticVerdict.NEGATIVE


def evidence_direction(contribution: Decimal) -> EvidenceDirection:
    """Map centered evidence contribution to a diagnostic direction."""
    contribution = _decimal(contribution, "evidence contribution")
    if contribution > 0:
        return EvidenceDirection.SUPPORTS
    if contribution < 0:
        return EvidenceDirection.OPPOSES
    return EvidenceDirection.NEUTRAL


def centered_evidence_contribution(score: Decimal) -> Decimal:
    """Center a zero-to-one-hundred score around neutral fifty."""
    score = _decimal(score, "component score")
    with localcontext(Context(prec=34)):
        return (score - Decimal("50")) / Decimal("50")


def recency_factor(
    known_at: datetime,
    latest_available_at: datetime,
    frequency: DataFrequency,
) -> Decimal:
    """Compute the published freshness factor without float conversion."""
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise SecFundamentalDiagnosticRuleError("known_at must include timezone information")
    if latest_available_at.tzinfo is None or latest_available_at.utcoffset() is None:
        raise SecFundamentalDiagnosticRuleError(
            "latest_available_at must include timezone information"
        )
    delta = known_at - latest_available_at
    if delta.days < 0:
        raise SecFundamentalDiagnosticRuleError("metric availability cannot be after known_at")
    with localcontext(Context(prec=34)):
        age_days = (
            Decimal(delta.days)
            + Decimal(delta.seconds) / Decimal("86400")
            + Decimal(delta.microseconds) / Decimal("86400000000")
        )
        if frequency is DataFrequency.QUARTERLY:
            fresh_limit = Decimal("150")
            stale_limit = Decimal("365")
        elif frequency is DataFrequency.ANNUAL:
            fresh_limit = Decimal("400")
            stale_limit = Decimal("800")
        else:
            raise SecFundamentalDiagnosticRuleError(
                "recency supports only annual or quarterly frequency"
            )
        if age_days <= fresh_limit:
            return Decimal("1.00")
        if age_days >= stale_limit:
            return Decimal("0.50")
        decline = (age_days - fresh_limit) / (stale_limit - fresh_limit)
        return Decimal("1.00") - decline * Decimal("0.50")


def confidence_for(coverage: Decimal, recency: Decimal) -> Decimal:
    """Combine coverage and recency into a non-probabilistic confidence value."""
    coverage = clamp(coverage, Decimal("0"), Decimal("1"))
    recency = clamp(recency, Decimal("0"), Decimal("1"))
    with localcontext(Context(prec=34)):
        return clamp(coverage * recency, Decimal("0"), Decimal("1"))


def quality_for(
    *,
    sufficient: bool,
    coverage: Decimal,
    recency: Decimal,
) -> DataQuality:
    """Map diagnostic sufficiency, coverage, and recency to existing quality enums."""
    if not sufficient:
        return DataQuality.PARTIAL
    if coverage == Decimal("1") and recency >= Decimal("0.75"):
        return DataQuality.VALID
    return DataQuality.PARTIAL


if sum(BASE_WEIGHTS.values(), Decimal("0")) != Decimal("1.00"):
    raise RuntimeError("fundamental diagnostic base weights must sum exactly to 1.00")
