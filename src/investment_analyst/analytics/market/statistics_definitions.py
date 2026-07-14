"""Auditable definitions for the first historical market statistics."""

from investment_analyst.core.models import MetricCategory, MetricDefinition

SIMPLE_RETURN_KEY = "market.history.simple_return_1d"
SMA_KEY = "market.history.sma"
VOLATILITY_KEY = "market.history.rolling_daily_volatility"
RELATIVE_VOLUME_KEY = "market.history.relative_volume"

_REFERENCE = "Internal auditable market-statistics specification."
_NO_ADVICE = "This descriptive statistic is not a financial recommendation."


def get_market_statistics_definitions() -> tuple[MetricDefinition, ...]:
    """Return the four versioned metric contracts supported by the engine."""
    return (
        MetricDefinition(
            metric_key=SIMPLE_RETURN_KEY,
            display_name="Daily Simple Return",
            category=MetricCategory.MARKET,
            description="Simple close-to-close return using the previous available bar.",
            formula="(close_t / close_previous_available_bar) - 1",
            unit="ratio",
            default_parameters={
                "periods": 1,
                "price_field": "close",
                "previous_bar_semantics": "previous_available_bar",
            },
            limitations=[
                "It does not necessarily represent one calendar day.",
                "For equities it uses the previous available trading session.",
                "For crypto it uses the previous available daily bar.",
                "Corporate events depend on the adjustment applied by the source.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=SMA_KEY,
            display_name="Simple Moving Average",
            category=MetricCategory.MARKET,
            description="Arithmetic mean of closing prices in an available-bar window.",
            formula="sum(close values in window) / window",
            unit="USD",
            default_parameters={
                "window": 20,
                "price_field": "close",
                "includes_current_bar": True,
            },
            limitations=[
                "Uses available bars without filling gaps or inferring a calendar.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=VOLATILITY_KEY,
            display_name="Rolling Daily Volatility",
            category=MetricCategory.MARKET,
            description="Sample standard deviation of recent simple returns.",
            formula="sample_standard_deviation(last N simple returns)",
            unit="ratio",
            default_parameters={
                "window": 20,
                "return_type": "simple",
                "degrees_of_freedom": 1,
                "annualized": False,
            },
            limitations=[
                "The result is not annualized.",
                "No square-root-of-252 or square-root-of-365 factor is applied.",
                "Requires window plus one available closing prices.",
                "Uses available bars without inferring a calendar.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=RELATIVE_VOLUME_KEY,
            display_name="Relative Volume",
            category=MetricCategory.MARKET,
            description="Current volume relative to the mean of previous available volumes.",
            formula="current_volume / mean(previous N available volumes)",
            unit="ratio",
            default_parameters={
                "window": 20,
                "comparison": "previous_available_bars",
                "excludes_current_bar_from_baseline": True,
            },
            limitations=[
                "Alpaca IEX volume is partial.",
                "Coinbase volume represents Coinbase Exchange only.",
                "It must not yet be interpreted as institutional activity.",
                "No result is emitted when historical mean volume is zero.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
    )
