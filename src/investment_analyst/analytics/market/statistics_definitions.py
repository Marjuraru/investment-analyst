"""Auditable definitions for the first historical market statistics."""

from investment_analyst.core.models import MetricCategory, MetricDefinition

SIMPLE_RETURN_KEY = "market.history.simple_return_1d"
SMA_KEY = "market.history.sma"
VOLATILITY_KEY = "market.history.rolling_daily_volatility"
RELATIVE_VOLUME_KEY = "market.history.relative_volume"
BOLLINGER_UPPER_KEY = "market.technical.bollinger.upper"
BOLLINGER_LOWER_KEY = "market.technical.bollinger.lower"
BOLLINGER_BANDWIDTH_KEY = "market.technical.bollinger.bandwidth"
BOLLINGER_PERCENT_B_KEY = "market.technical.bollinger.percent_b"
EMA_KEY = "market.technical.ema"

_REFERENCE = "Internal auditable market-statistics specification."
_NO_ADVICE = "This descriptive statistic is not a financial recommendation."


def get_market_statistics_definitions() -> tuple[MetricDefinition, ...]:
    """Return the versioned metric contracts supported by the engine."""
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
        MetricDefinition(
            metric_key=EMA_KEY,
            display_name="Exponential Moving Average",
            category=MetricCategory.MARKET,
            description=(
                "Recursive exponential moving average of available closing prices seeded by the "
                "first in-query simple moving average."
            ),
            formula=(
                "ema_t = alpha * close_t + (1 - alpha) * ema_previous; alpha = 2 / (window + 1)"
            ),
            unit="USD",
            default_parameters={
                "window": 20,
                "alpha": "0.09523809523809523809523809523809524",
                "seed_method": "sma_first_window",
                "price_field": "close",
                "includes_current_bar": True,
            },
            limitations=[
                "The seed and recurrence use only bars selected by the point-in-time query.",
                "Uses available bars without filling gaps or inferring a calendar.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=BOLLINGER_UPPER_KEY,
            display_name="Bollinger Upper Band",
            category=MetricCategory.MARKET,
            description="Upper population-standard-deviation band of available closing prices.",
            formula="mean(close window) + multiplier * sqrt(sum((close - mean)^2) / window)",
            unit="USD",
            default_parameters={
                "window": 20,
                "multiplier": "2",
                "price_field": "close",
                "degrees_of_freedom": 0,
                "includes_current_bar": True,
            },
            limitations=[
                "Uses population standard deviation, not sample standard deviation.",
                "Uses available bars without filling gaps or inferring a calendar.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=BOLLINGER_LOWER_KEY,
            display_name="Bollinger Lower Band",
            category=MetricCategory.MARKET,
            description="Lower population-standard-deviation band of available closing prices.",
            formula="mean(close window) - multiplier * sqrt(sum((close - mean)^2) / window)",
            unit="USD",
            default_parameters={
                "window": 20,
                "multiplier": "2",
                "price_field": "close",
                "degrees_of_freedom": 0,
                "includes_current_bar": True,
            },
            limitations=["The middle band is the existing SMA with the same window.", _NO_ADVICE],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=BOLLINGER_BANDWIDTH_KEY,
            display_name="Bollinger Bandwidth",
            category=MetricCategory.MARKET,
            description="Relative distance between the upper and lower Bollinger bands.",
            formula="(upper - lower) / middle",
            unit="ratio",
            default_parameters={"window": 20, "multiplier": "2", "price_field": "close"},
            limitations=["The middle band is the existing SMA with the same window.", _NO_ADVICE],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
        MetricDefinition(
            metric_key=BOLLINGER_PERCENT_B_KEY,
            display_name="Bollinger Percent B",
            category=MetricCategory.MARKET,
            description=(
                "Close position within the Bollinger band when the band has non-zero width."
            ),
            formula="(close - lower) / (upper - lower)",
            unit="ratio",
            default_parameters={"window": 20, "multiplier": "2", "price_field": "close"},
            limitations=[
                "No value is emitted for a flat band because the denominator is zero.",
                _NO_ADVICE,
            ],
            references=[_REFERENCE],
            definition_version="1.0.0",
        ),
    )
