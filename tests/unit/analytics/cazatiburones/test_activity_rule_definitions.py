from decimal import Decimal

import pytest

from investment_analyst.analytics.cazatiburones.activity_rule_definitions import (
    ACTIVITY_RULE_CATALOG,
    DEFINITION_VERSION,
    _build_catalog,
)


def test_catalog_is_versioned_and_covers_the_three_families() -> None:
    assert ACTIVITY_RULE_CATALOG
    assert all(rule.definition_version == DEFINITION_VERSION for rule in ACTIVITY_RULE_CATALOG)
    families = {rule.family for rule in ACTIVITY_RULE_CATALOG}
    assert families == {"insider", "beneficial_ownership", "institutional_13f"}
    rule_ids = [rule.rule_id for rule in ACTIVITY_RULE_CATALOG]
    assert len(rule_ids) == len(set(rule_ids))


def _spec(**overrides: object) -> dict[str, object]:
    base = dict(
        rule_id="test-rule",
        family="insider",
        forms=("4",),
        feature_key="acquisition_count",
        condition_type="decimal_compare",
        comparator="eq",
        threshold=Decimal("1"),
        unit="count",
        label_es="etiqueta",
        limitations="ninguna",
        definition_version=DEFINITION_VERSION,
    )
    base.update(overrides)
    return base


def test_catalog_rejects_an_undeclared_feature_key() -> None:
    with pytest.raises(ValueError, match="is not declared"):
        _build_catalog((_spec(feature_key="not_a_real_feature"),))


def test_catalog_rejects_a_declared_unit_mismatch() -> None:
    with pytest.raises(ValueError, match="conflicts with"):
        _build_catalog((_spec(unit="ratio"),))


def test_catalog_rejects_boolean_condition_on_numeric_feature() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        _build_catalog(
            (
                _spec(
                    feature_key="threshold_appearance",
                    family="beneficial_ownership",
                    condition_type="decimal_compare",
                    comparator="eq",
                    threshold=Decimal("1"),
                    unit="boolean",
                ),
            )
        )


def test_catalog_rejects_numeric_condition_on_boolean_feature() -> None:
    with pytest.raises(ValueError, match="requires expected_value"):
        _build_catalog(
            (
                _spec(
                    feature_key="threshold_appearance",
                    family="beneficial_ownership",
                    condition_type="boolean_equals",
                    comparator="eq",
                    threshold=None,
                    unit="boolean",
                ),
            )
        )


def test_catalog_rejects_a_non_finite_threshold() -> None:
    with pytest.raises(ValueError, match="finite"):
        _build_catalog((_spec(threshold=Decimal("NaN")),))
    with pytest.raises(ValueError, match="finite"):
        _build_catalog((_spec(threshold=Decimal("Infinity")),))


def test_catalog_rejects_a_float_threshold() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic strict-mode ValidationError
        _build_catalog((_spec(threshold=1.0),))


def test_catalog_rejects_duplicate_rule_ids() -> None:
    with pytest.raises(ValueError, match="duplicate rule_id"):
        _build_catalog((_spec(), _spec(feature_key="disposition_count")))
