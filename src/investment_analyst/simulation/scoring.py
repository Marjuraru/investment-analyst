"""Pure Decimal scoring rules for the technical simulation."""

from decimal import Decimal

from investment_analyst.core.models import DiagnosticVerdict

ZERO = Decimal("0")
ONE = Decimal("1")
ONE_HUNDRED = Decimal("100")
RETURN_WEIGHT = Decimal("0.70")
VOLUME_WEIGHT = Decimal("0.30")


def clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    """Limit a Decimal value to an inclusive range."""
    if minimum > maximum:
        raise ValueError("minimum must not be greater than maximum")
    return min(max(value, minimum), maximum)


def score_return(simple_return: Decimal) -> Decimal:
    """Map a one-day simple return to the demonstration score range."""
    return clamp(Decimal("50") + simple_return * Decimal("1000"), ZERO, ONE_HUNDRED)


def score_volume(volume_ratio: Decimal) -> Decimal:
    """Map a one-day volume ratio to the demonstration score range."""
    return clamp(
        Decimal("50") + (volume_ratio - ONE) * Decimal("25"),
        ZERO,
        ONE_HUNDRED,
    )


def final_score(return_score: Decimal, volume_score: Decimal) -> Decimal:
    """Combine explicit return and volume scores without rounding."""
    return return_score * RETURN_WEIGHT + volume_score * VOLUME_WEIGHT


def verdict_for_score(score: Decimal) -> DiagnosticVerdict:
    """Return the demonstration verdict for a final score."""
    if score >= Decimal("60"):
        return DiagnosticVerdict.POSITIVE
    if score <= Decimal("40"):
        return DiagnosticVerdict.NEGATIVE
    return DiagnosticVerdict.NEUTRAL
