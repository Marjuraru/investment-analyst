# ruff: noqa: E501
"""Versioned catalog of persistible cazatiburones activity metrics (layer 3).

Only metrics declared here may ever be emitted by the engine or persisted by the pipeline.
Each entry names the exact layer-2 observation fields it consumes; a metric with no
normalized-observation backing has no entry and cannot be computed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.core.models.enums import MetricCategory
from investment_analyst.core.models.metric import MetricDefinition

DEFINITION_VERSION = "cazatiburones-activity-metrics-v1"
ALGORITHM_VERSION = "cazatiburones-activity-metrics-v1"

ActivityMetricFamily = Literal["insider", "beneficial_ownership"]


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class ActivityMetricCatalogEntry(_Strict):
    """Versioned, human-readable description of one persistible activity metric."""

    metric_key: NonEmptyStr
    family: ActivityMetricFamily
    display_name: NonEmptyStr
    formula: NonEmptyStr
    unit: NonEmptyStr
    layer2_fields: tuple[NonEmptyStr, ...]
    required_observation_count: int
    limitations: tuple[NonEmptyStr, ...]
    references: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_shape(self) -> ActivityMetricCatalogEntry:
        if not self.layer2_fields:
            raise ValueError("activity metric catalog entry requires at least one layer-2 field")
        if self.required_observation_count != 2:
            raise ValueError("activity metric catalog entries require exactly two observations")
        return self


ACTIVITY_METRIC_CATALOG: tuple[ActivityMetricCatalogEntry, ...] = (
    ActivityMetricCatalogEntry(
        metric_key="cazatiburones.insider.holding_delta_ratio",
        family="insider",
        display_name="Insider holding delta ratio",
        formula="(post_holding - prior_holding) / prior_holding",
        unit="ratio",
        layer2_fields=("insider_shares_owned_following",),
        required_observation_count=2,
        limitations=(
            "requires two consecutive declared holdings for the same participant, "
            "security title, and table",
            "not evaluable when the prior declared holding is zero or absent",
        ),
        references=("Form 3/4/5, Table I/II, column 5",),
    ),
    ActivityMetricCatalogEntry(
        metric_key="cazatiburones.beneficial.delta_percent_of_class",
        family="beneficial_ownership",
        display_name="Beneficial ownership percent-of-class delta",
        formula="percent_of_class_current - percent_of_class_previous",
        unit="percentage_points",
        layer2_fields=("beneficial_percent_of_class",),
        required_observation_count=2,
        limitations=(
            "requires two consecutive declarations for the same subject and reporting person",
        ),
        references=("Schedule 13D/13G, Item 5",),
    ),
    ActivityMetricCatalogEntry(
        metric_key="cazatiburones.beneficial.delta_shares_beneficially_owned",
        family="beneficial_ownership",
        display_name="Beneficial ownership shares delta",
        formula="shares_beneficially_owned_current - shares_beneficially_owned_previous",
        unit="shares",
        layer2_fields=("beneficial_shares_owned",),
        required_observation_count=2,
        limitations=(
            "requires two consecutive declarations for the same subject and reporting person",
        ),
        references=("Schedule 13D/13G, Item 5",),
    ),
)

if len({entry.metric_key for entry in ACTIVITY_METRIC_CATALOG}) != len(ACTIVITY_METRIC_CATALOG):
    raise AssertionError("activity metric catalog has duplicate metric keys")

_CATALOG_BY_KEY: dict[str, ActivityMetricCatalogEntry] = {
    entry.metric_key: entry for entry in ACTIVITY_METRIC_CATALOG
}

INSIDER_LAYER2_FIELDS: frozenset[str] = frozenset(
    field_name
    for entry in ACTIVITY_METRIC_CATALOG
    if entry.family == "insider"
    for field_name in entry.layer2_fields
)
BENEFICIAL_LAYER2_FIELDS: frozenset[str] = frozenset(
    field_name
    for entry in ACTIVITY_METRIC_CATALOG
    if entry.family == "beneficial_ownership"
    for field_name in entry.layer2_fields
)


def get_activity_metric_catalog_entry(metric_key: str) -> ActivityMetricCatalogEntry:
    """Return the catalog entry for one persistible metric key."""
    try:
        return _CATALOG_BY_KEY[metric_key]
    except KeyError as error:
        raise ValueError(f"activity metric {metric_key!r} is not defined") from error


def build_metric_definition(entry: ActivityMetricCatalogEntry) -> MetricDefinition:
    """Project one catalog entry to its persistable MetricDefinition."""
    return MetricDefinition(
        metric_key=entry.metric_key,
        display_name=entry.display_name,
        category=MetricCategory.CAZATIBURONES,
        description=entry.display_name,
        formula=entry.formula,
        unit=entry.unit,
        default_parameters={},
        limitations=list(entry.limitations),
        references=list(entry.references),
        definition_version=DEFINITION_VERSION,
    )


ACTIVITY_METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = tuple(
    build_metric_definition(entry) for entry in ACTIVITY_METRIC_CATALOG
)
