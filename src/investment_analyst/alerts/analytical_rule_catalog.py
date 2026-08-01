"""Versioned silent templates for the first analytical screening iteration."""

from decimal import Decimal

from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionOperator,
    AnalyticalRuleState,
    AnalyticalScreeningCondition,
    AnalyticalScreeningDomain,
    AnalyticalScreeningRule,
)
from investment_analyst.core.models import AssetClass, DataQuality

INITIAL_MARKET_ACTIVITY_RULE = AnalyticalScreeningRule(
    rule_id="market.activity.relative-volume-review",
    rule_version="1.0",
    name_es="Actividad relativa para revisión",
    description_es=("Detecta volumen diario relativo elevado frente a las 20 barras anteriores."),
    state=AnalyticalRuleState.SILENT,
    domain=AnalyticalScreeningDomain.MARKET,
    asset_classes=(AssetClass.CRYPTO, AssetClass.EQUITY, AssetClass.ETF),
    conditions=(
        AnalyticalScreeningCondition(
            condition_id="relative_volume_gte_1_5",
            label_es="Volumen relativo",
            domain=AnalyticalScreeningDomain.MARKET,
            metric_key="market.history.relative_volume",
            algorithm_version="market-relative-volume-v1-decimal34",
            unit="ratio",
            operator=AnalyticalConditionOperator.GREATER_THAN_OR_EQUAL,
            threshold=Decimal("1.5"),
            exit_threshold=Decimal("1.2"),
            parameter_filters={"window": 20},
            accepted_qualities=(DataQuality.PARTIAL, DataQuality.VALID),
            limitations=(
                "Alpaca IEX cubre una sola bolsa y su volumen no representa el SIP consolidado.",
                "Coinbase representa únicamente la actividad observada en Coinbase Exchange.",
            ),
        ),
    ),
    confirmations_required=2,
    cooldown_seconds=86_400,
    limitations=(
        "La expansión de volumen es una condición descriptiva, no una señal de compra o venta.",
    ),
)

INITIAL_QUARTERLY_FUNDAMENTAL_RULE = AnalyticalScreeningRule(
    rule_id="fundamentals.quarterly-balance-growth-review",
    rule_version="1.0",
    name_es="Balance, margen y crecimiento para revisión",
    description_es=(
        "Evalúa simultáneamente tres condiciones fundamentales trimestrales configuradas."
    ),
    state=AnalyticalRuleState.SILENT,
    domain=AnalyticalScreeningDomain.FUNDAMENTALS,
    asset_classes=(AssetClass.EQUITY,),
    conditions=(
        AnalyticalScreeningCondition(
            condition_id="liabilities_to_assets_lte_0_6",
            label_es="Pasivos sobre activos",
            domain=AnalyticalScreeningDomain.FUNDAMENTALS,
            metric_key="fundamental.liabilities_to_assets",
            algorithm_version="sec-fundamental-liabilities-to-assets-v1-decimal34",
            unit="ratio",
            operator=AnalyticalConditionOperator.LESS_THAN_OR_EQUAL,
            threshold=Decimal("0.6"),
            parameter_filters={"frequency": "quarterly"},
        ),
        AnalyticalScreeningCondition(
            condition_id="net_margin_gt_0",
            label_es="Margen neto",
            domain=AnalyticalScreeningDomain.FUNDAMENTALS,
            metric_key="fundamental.net_margin",
            algorithm_version="sec-fundamental-net-margin-v1-decimal34",
            unit="ratio",
            operator=AnalyticalConditionOperator.GREATER_THAN,
            threshold=Decimal("0"),
            parameter_filters={"frequency": "quarterly"},
        ),
        AnalyticalScreeningCondition(
            condition_id="revenue_yoy_growth_gt_0",
            label_es="Crecimiento interanual de ingresos",
            domain=AnalyticalScreeningDomain.FUNDAMENTALS,
            metric_key="fundamental.revenue_yoy_growth",
            algorithm_version="sec-fundamental-revenue-yoy-growth-v1-decimal34",
            unit="ratio",
            operator=AnalyticalConditionOperator.GREATER_THAN,
            threshold=Decimal("0"),
            parameter_filters={"frequency": "quarterly"},
        ),
    ),
    confirmations_required=1,
    cooldown_seconds=604_800,
    limitations=(
        "Los umbrales son una plantilla inicial configurable, no una definición universal "
        "de calidad.",
        "La regla no incorpora valoración, precio, sector ni contexto macro.",
    ),
)

INITIAL_ANALYTICAL_RULES = (
    INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
    INITIAL_MARKET_ACTIVITY_RULE,
)

__all__ = [
    "INITIAL_ANALYTICAL_RULES",
    "INITIAL_MARKET_ACTIVITY_RULE",
    "INITIAL_QUARTERLY_FUNDAMENTAL_RULE",
]
