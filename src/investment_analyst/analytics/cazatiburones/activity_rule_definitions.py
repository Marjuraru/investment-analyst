# ruff: noqa: E501
"""Versioned catalog of descriptive activity rules, per form family.

Every rule is validated against the already-integrated feature catalogs of the
declared-activity (Forms 3/4/5, Schedules 13D/13G) and institutional-change (Form 13F)
verticals before it is exposed. A rule that names an undeclared feature key, or whose
condition type conflicts with that feature's declared unit, fails to build the module
rather than being evaluated silently.
"""

from decimal import Decimal
from typing import Any

from investment_analyst.analytics.cazatiburones.activity_rule_models import ActivityRuleDefinition
from investment_analyst.analytics.cazatiburones.declared_activity_definitions import (
    FEATURE_DEFINITIONS,
)
from investment_analyst.analytics.cazatiburones.institutional_change_definitions import (
    METRIC_DEFINITIONS,
    MINIMUM_BASELINE_SAMPLE,
)

DEFINITION_VERSION = "cazatiburones-activity-rules-v1"

_DECLARED_ACTIVITY_FEATURE_INDEX = {feature.key: feature for feature in FEATURE_DEFINITIONS}

_INSIDER_FORMS = ("3", "3/A", "4", "4/A", "5", "5/A")
_BENEFICIAL_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")
_INSTITUTIONAL_FORMS = ("13F-HR", "13F-HR/A")

# Existential rules over `entry`/`exit` intentionally do not identify a single security:
# `InstitutionalChangeResult.metrics` flattens every changed position into one tuple
# without a CUSIP or title_of_class tag, so a per-position identity cannot be recovered
# from the reused contract. See docs/cazatiburones_activity_rules.md.
_RULE_SPECS: tuple[dict[str, Any], ...] = (
    dict(
        rule_id="insider-acquisition-declared",
        family="insider",
        forms=_INSIDER_FORMS,
        feature_key="acquisition_count",
        condition_type="decimal_compare",
        comparator="eq",
        threshold=Decimal("1"),
        unit="count",
        label_es="Adquisición declarada (código A)",
        limitations="declared transaction code only; a single-entry indicator, not a magnitude",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="insider-disposition-declared",
        family="insider",
        forms=_INSIDER_FORMS,
        feature_key="disposition_count",
        condition_type="decimal_compare",
        comparator="eq",
        threshold=Decimal("1"),
        unit="count",
        label_es="Disposición declarada (código D)",
        limitations="declared transaction code only; a single-entry indicator, not a magnitude",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="insider-holding-reduced",
        family="insider",
        forms=_INSIDER_FORMS,
        feature_key="holding_delta_ratio",
        condition_type="decimal_compare",
        comparator="lt",
        threshold=Decimal("0"),
        unit="ratio",
        label_es="Reducción de tenencia frente a la previa declarada",
        limitations="missing for zero or absent prior; requires a preceding declared statement for the same owner/security/table",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="insider-clustered-activity",
        family="insider",
        forms=_INSIDER_FORMS,
        feature_key="clustered_transaction_count",
        condition_type="decimal_compare",
        comparator="gte",
        threshold=Decimal("2"),
        unit="count",
        label_es="Agrupación temporal de transacciones declaradas",
        limitations="counts same participant/security/table transactions within a declared 30 calendar-day window, including itself; describes temporal clustering, not intent",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="insider-filing-delay-beyond-threshold",
        family="insider",
        forms=_INSIDER_FORMS,
        feature_key="filing_delay_days",
        condition_type="decimal_compare",
        comparator="gt",
        threshold=Decimal("2"),
        unit="days",
        label_es="Demora de presentación más allá del umbral declarado",
        limitations="full calendar days between available_at and the declared transaction date; purely descriptive, never a legal compliance judgment; missing without a declared transaction date",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="beneficial-threshold-appearance",
        family="beneficial_ownership",
        forms=_BENEFICIAL_FORMS,
        feature_key="threshold_appearance",
        condition_type="boolean_equals",
        expected_value=True,
        unit="boolean",
        label_es="Aparición de umbral declarado de propiedad beneficiaria",
        limitations="declared presence only; requires a preceding declared statement for the same subject/reporting-person pair",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="beneficial-threshold-exit",
        family="beneficial_ownership",
        forms=_BENEFICIAL_FORMS,
        feature_key="threshold_exit",
        condition_type="boolean_equals",
        expected_value=True,
        unit="boolean",
        label_es="Salida de umbral declarado de propiedad beneficiaria",
        limitations="declared presence only; requires a preceding declared statement for the same subject/reporting-person pair",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="beneficial-percent-increase",
        family="beneficial_ownership",
        forms=_BENEFICIAL_FORMS,
        feature_key="delta_percent_of_class",
        condition_type="decimal_compare",
        comparator="gt",
        threshold=Decimal("0"),
        unit="percentage_points",
        label_es="Variación al alza del porcentaje de clase declarado",
        limitations="consecutive declarations only for the same subject/reporting-person pair",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="beneficial-percent-decrease",
        family="beneficial_ownership",
        forms=_BENEFICIAL_FORMS,
        feature_key="delta_percent_of_class",
        condition_type="decimal_compare",
        comparator="lt",
        threshold=Decimal("0"),
        unit="percentage_points",
        label_es="Variación a la baja del porcentaje de clase declarado",
        limitations="consecutive declarations only for the same subject/reporting-person pair",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="institutional-position-entry",
        family="institutional_13f",
        forms=_INSTITUTIONAL_FORMS,
        feature_key="entry",
        condition_type="boolean_equals",
        expected_value=True,
        unit="boolean",
        label_es="Aparición de una posición declarada en el 13F",
        limitations="true when any position in the manager's declared 13F comparison newly appears; the reused contract does not preserve per-position CUSIP identity, so this rule cannot identify which security changed and is not a cross-security score",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="institutional-position-exit",
        family="institutional_13f",
        forms=_INSTITUTIONAL_FORMS,
        feature_key="exit",
        condition_type="boolean_equals",
        expected_value=True,
        unit="boolean",
        label_es="Salida de una posición declarada en el 13F",
        limitations="true when any position in the manager's declared 13F comparison newly disappears; the reused contract does not preserve per-position CUSIP identity, so this rule cannot identify which security changed and is not a cross-security score",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="institutional-position-concentration",
        family="institutional_13f",
        forms=_INSTITUTIONAL_FORMS,
        feature_key="position_concentration",
        condition_type="decimal_compare",
        comparator="gt",
        threshold=Decimal("0.10"),
        unit="ratio",
        label_es="Concentración de la posición más grande declarada",
        limitations="single largest reported position value over the declared portfolio total; missing when declared_value_total is absent or zero",
        definition_version=DEFINITION_VERSION,
    ),
    dict(
        rule_id="institutional-robust-percentile-high",
        family="institutional_13f",
        forms=_INSTITUTIONAL_FORMS,
        feature_key="robust_percentile",
        condition_type="decimal_compare",
        comparator="gt",
        threshold=Decimal("0.90"),
        unit="ratio",
        minimum_sample=MINIMUM_BASELINE_SAMPLE,
        label_es="Percentil robusto elevado frente al historial declarado",
        limitations=(
            f"requires a historical value series built by "
            "institutional_change_engine.robust_baseline with at least "
            f"{MINIMUM_BASELINE_SAMPLE} observations; InstitutionalChangeService.query's "
            "pairwise comparison does not currently invoke that function, so this rule is "
            "always not_evaluable pending that upstream wiring; declared here for "
            "forward-compatible cataloging per institutional-change-definitions-v1"
        ),
        definition_version=DEFINITION_VERSION,
    ),
)


def _declared_unit(family: str, feature_key: str) -> str:
    if family == "institutional_13f":
        try:
            return METRIC_DEFINITIONS[feature_key][1]
        except KeyError as error:
            raise ValueError(
                f"feature_key '{feature_key}' is not declared for family '{family}'"
            ) from error
    definition = _DECLARED_ACTIVITY_FEATURE_INDEX.get(feature_key)
    if definition is None:
        raise ValueError(f"feature_key '{feature_key}' is not declared for family '{family}'")
    return definition.unit


def _build_catalog(specs: tuple[dict[str, Any], ...]) -> tuple[ActivityRuleDefinition, ...]:
    seen_ids: set[str] = set()
    catalog: list[ActivityRuleDefinition] = []
    for spec in specs:
        definition = ActivityRuleDefinition(**spec)
        if definition.rule_id in seen_ids:
            raise ValueError(f"duplicate rule_id '{definition.rule_id}' in activity rule catalog")
        seen_ids.add(definition.rule_id)
        declared_unit = _declared_unit(definition.family, definition.feature_key)
        if declared_unit != definition.unit:
            raise ValueError(
                f"rule '{definition.rule_id}' unit '{definition.unit}' conflicts with "
                f"declared feature unit '{declared_unit}'"
            )
        expects_boolean = declared_unit == "boolean"
        if expects_boolean != (definition.condition_type == "boolean_equals"):
            raise ValueError(
                f"rule '{definition.rule_id}' condition_type is incompatible with "
                f"feature unit '{declared_unit}'"
            )
        catalog.append(definition)
    return tuple(catalog)


ACTIVITY_RULE_CATALOG: tuple[ActivityRuleDefinition, ...] = _build_catalog(_RULE_SPECS)
