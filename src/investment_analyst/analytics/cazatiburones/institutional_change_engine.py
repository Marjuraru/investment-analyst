"""Pure Decimal-exact calculations; no persistence or provider access."""

from decimal import Decimal
from statistics import median

from investment_analyst.analytics.cazatiburones.institutional_change_definitions import (
    DEFAULT_TOP_N,
    MINIMUM_BASELINE_SAMPLE,
    ZERO,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import (
    DescriptiveMetric,
    InstitutionalChangeResult,
    InstitutionalClose,
)


def compare(previous: InstitutionalClose, current: InstitutionalClose) -> InstitutionalChangeResult:
    if previous.manager_cik != current.manager_cik:
        raise ValueError("institutional closes must have the same manager")
    if previous.report_period >= current.report_period:
        raise ValueError("institutional closes must be ordered by period")
    old = {(p.cusip, p.title_of_class): p for p in previous.positions}
    new = {(p.cusip, p.title_of_class): p for p in current.positions}
    metrics: list[DescriptiveMetric] = []
    for key in sorted(old | new):
        before, after = old.get(key), new.get(key)
        metrics.extend(_position_metrics(before, after))
    metrics.extend(_concentration_metrics(current))
    return InstitutionalChangeResult(
        manager_cik=current.manager_cik,
        previous_period=previous.report_period,
        current_period=current.report_period,
        available_at=max(previous.available_at, current.available_at),
        metrics=tuple(metrics),
    )


def robust_baseline(values: tuple[Decimal, ...], current: Decimal) -> tuple[DescriptiveMetric, ...]:
    if len(values) < MINIMUM_BASELINE_SAMPLE:
        return tuple(
            DescriptiveMetric(key=key, status="not_evaluable")
            for key in ("robust_median", "robust_mad", "robust_percentile")
        )
    mid = Decimal(str(median(values)))
    mad = Decimal(str(median(tuple(abs(value - mid) for value in values))))
    return (
        DescriptiveMetric(key="robust_median", status="available", value=mid),
        DescriptiveMetric(key="robust_mad", status="available", value=mad),
        DescriptiveMetric(
            key="robust_percentile",
            status="available",
            value=Decimal(sum(value <= current for value in values)) / Decimal(len(values)),
        ),
    )


def _position_metrics(before, after) -> tuple[DescriptiveMetric, ...]:
    missing = (
        before is None
        or after is None
        or before.quantity is None
        or after.quantity is None
        or before.value is None
        or after.value is None
    )
    return (
        DescriptiveMetric(
            key="delta_quantity",
            status="missing" if missing else "available",
            value=None if missing else after.quantity - before.quantity,
        ),
        DescriptiveMetric(
            key="delta_value",
            status="missing" if missing else "available",
            value=None if missing else after.value - before.value,
        ),
        DescriptiveMetric(
            key="entry", status="available", value=before is None and after is not None
        ),
        DescriptiveMetric(
            key="exit", status="available", value=before is not None and after is None
        ),
    )


def _concentration_metrics(current: InstitutionalClose) -> tuple[DescriptiveMetric, ...]:
    total = current.declared_value_total
    if total is None or total == ZERO:
        return tuple(
            DescriptiveMetric(key=key, status="missing")
            for key in ("position_concentration", "portfolio_top_n_concentration")
        )
    values = tuple(position.value for position in current.positions if position.value is not None)
    return (
        DescriptiveMetric(
            key="position_concentration",
            status="available",
            value=max(values, default=ZERO) / total,
        ),
        DescriptiveMetric(
            key="portfolio_top_n_concentration",
            status="available",
            value=sum(sorted(values, reverse=True)[:DEFAULT_TOP_N], ZERO) / total,
        ),
    )
