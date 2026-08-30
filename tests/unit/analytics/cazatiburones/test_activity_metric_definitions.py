import pytest

from investment_analyst.analytics.cazatiburones.activity_metric_definitions import (
    ACTIVITY_METRIC_CATALOG,
    ACTIVITY_METRIC_DEFINITIONS,
    ALGORITHM_VERSION,
    DEFINITION_VERSION,
    get_activity_metric_catalog_entry,
)
from investment_analyst.core.models.enums import MetricCategory


def test_catalog_declares_exactly_the_three_scoped_metrics() -> None:
    assert {entry.metric_key for entry in ACTIVITY_METRIC_CATALOG} == {
        "cazatiburones.insider.holding_delta_ratio",
        "cazatiburones.beneficial.delta_percent_of_class",
        "cazatiburones.beneficial.delta_shares_beneficially_owned",
    }


def test_catalog_entries_declare_layer2_inputs_and_two_observations() -> None:
    for entry in ACTIVITY_METRIC_CATALOG:
        assert entry.layer2_fields
        assert entry.required_observation_count == 2
        assert entry.limitations
        assert entry.references


def test_definitions_are_versioned_cazatiburones_metric_definitions() -> None:
    assert len(ACTIVITY_METRIC_DEFINITIONS) == 3
    for definition in ACTIVITY_METRIC_DEFINITIONS:
        assert definition.category == MetricCategory.CAZATIBURONES
        assert definition.definition_version == DEFINITION_VERSION
    assert ALGORITHM_VERSION == DEFINITION_VERSION


def test_unknown_metric_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="not defined"):
        get_activity_metric_catalog_entry("cazatiburones.unknown.metric")


def test_catalog_excludes_boolean_and_unbacked_features() -> None:
    excluded = {
        "acquisition_count",
        "disposition_count",
        "clustered_transaction_count",
        "participant_recurrence",
        "is_amendment",
        "filing_delay_days",
        "threshold_appearance",
        "threshold_exit",
        "transaction_shares",
        "prior_holding",
        "post_holding",
    }
    metric_suffixes = {entry.metric_key.rsplit(".", 1)[-1] for entry in ACTIVITY_METRIC_CATALOG}
    assert metric_suffixes.isdisjoint(excluded)


def test_catalog_excludes_form_13f() -> None:
    for entry in ACTIVITY_METRIC_CATALOG:
        assert "13f" not in entry.metric_key.lower()
        assert "13f" not in entry.formula.lower()
