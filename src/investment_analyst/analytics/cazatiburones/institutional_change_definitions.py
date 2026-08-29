"""Versioned descriptive definitions for institutional 13F changes."""

from decimal import Decimal

DEFINITION_VERSION = "institutional-change-definitions-v1"
MINIMUM_BASELINE_SAMPLE = 3
DEFAULT_TOP_N = 10
METRIC_DEFINITIONS = {
    "delta_quantity": ("current.quantity - previous.quantity", "reported_quantity"),
    "delta_value": ("current.value - previous.value", "reported_13f_value"),
    "entry": ("present(current) and absent(previous)", "boolean"),
    "exit": ("absent(current) and present(previous)", "boolean"),
    "position_concentration": ("position.value / report.declared_value_total", "ratio"),
    "portfolio_top_n_concentration": ("sum(top_n.values) / report.declared_value_total", "ratio"),
    "robust_median": ("median(history)", "reported_13f_value"),
    "robust_mad": ("median(abs(history - median(history)))", "reported_13f_value"),
    "robust_percentile": ("count(history <= current) / len(history)", "ratio"),
}
ZERO = Decimal("0")
