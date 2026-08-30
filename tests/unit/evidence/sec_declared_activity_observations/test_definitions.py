import pytest

from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
)
from investment_analyst.evidence.sec_declared_activity_observations.definitions import (
    CATALOG_VERSION,
    FIELD_DEFINITIONS,
    DeclaredActivityDateSource,
    DeclaredActivityFieldDefinition,
    get_field_definition,
    get_field_definitions_for_family,
)
from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_SOURCE_ID

_EXPECTED_INSIDER_FIELDS = {
    "insider_transaction_shares",
    "insider_transaction_price_per_share",
    "insider_shares_owned_following",
}
_EXPECTED_BENEFICIAL_FIELDS = {
    "beneficial_shares_owned",
    "beneficial_percent_of_class",
}


def test_catalog_covers_exactly_the_declared_fields_and_no_derived_field() -> None:
    field_names = {definition.field_name for definition in FIELD_DEFINITIONS}
    assert field_names == _EXPECTED_INSIDER_FIELDS | _EXPECTED_BENEFICIAL_FIELDS
    assert len(FIELD_DEFINITIONS) == len(field_names)


def test_every_field_declares_a_complete_versioned_definition() -> None:
    for definition in FIELD_DEFINITIONS:
        assert definition.field_name
        assert definition.family in {"insider", "beneficial_ownership"}
        assert definition.applicable_forms
        assert definition.source_id
        assert definition.source_attribute
        assert definition.unit
        assert definition.frequency is DataFrequency.EVENT
        assert definition.date_sources
        assert definition.quality is DataQuality.VALID
        assert definition.limitations
        assert definition.catalog_version == CATALOG_VERSION


def test_insider_fields_use_the_ownership_source_and_forms() -> None:
    for definition in get_field_definitions_for_family("insider"):
        assert definition.field_name in _EXPECTED_INSIDER_FIELDS
        assert definition.source_id == OWNERSHIP_SOURCE_ID
        assert definition.source_attribute.startswith("entry.")


def test_beneficial_fields_use_the_beneficial_ownership_source_and_forms() -> None:
    for definition in get_field_definitions_for_family("beneficial_ownership"):
        assert definition.field_name in _EXPECTED_BENEFICIAL_FIELDS
        assert definition.source_id == BENEFICIAL_OWNERSHIP_SOURCE_ID
        assert definition.source_attribute.startswith("statement.")


def test_get_field_definition_returns_declared_field() -> None:
    definition = get_field_definition("insider_transaction_shares")
    assert definition.field_name == "insider_transaction_shares"


def test_get_field_definition_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="not defined"):
        get_field_definition("insider_market_value")


def test_field_definition_rejects_entry_scoped_beneficial_source_attribute() -> None:
    with pytest.raises(ValueError, match="no entries"):
        DeclaredActivityFieldDefinition(
            field_name="beneficial_bad",
            family="beneficial_ownership",
            applicable_forms=("SC 13D",),
            source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
            source_attribute="entry.shares",
            unit="shares",
            frequency=DataFrequency.EVENT,
            date_sources=(
                DeclaredActivityDateSource(attribute="statement.event_date", target="observed_at"),
            ),
            quality=DataQuality.VALID,
            limitations="invalid",
            catalog_version=CATALOG_VERSION,
        )


def test_field_definition_rejects_entry_scoped_beneficial_date_source() -> None:
    with pytest.raises(ValueError, match="no entries"):
        DeclaredActivityFieldDefinition(
            field_name="beneficial_bad",
            family="beneficial_ownership",
            applicable_forms=("SC 13D",),
            source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
            source_attribute="statement.percent_of_class",
            unit="percent",
            frequency=DataFrequency.EVENT,
            date_sources=(
                DeclaredActivityDateSource(
                    attribute="entry.transaction_date", target="observed_at"
                ),
            ),
            quality=DataQuality.VALID,
            limitations="invalid",
            catalog_version=CATALOG_VERSION,
        )


def test_field_definition_requires_at_least_one_applicable_form_and_date_source() -> None:
    with pytest.raises(ValueError, match="applicable form"):
        DeclaredActivityFieldDefinition(
            field_name="x",
            family="insider",
            applicable_forms=(),
            source_id=OWNERSHIP_SOURCE_ID,
            source_attribute="entry.shares",
            unit="shares",
            frequency=DataFrequency.EVENT,
            date_sources=(
                DeclaredActivityDateSource(
                    attribute="entry.transaction_date", target="observed_at"
                ),
            ),
            quality=DataQuality.VALID,
            limitations="invalid",
            catalog_version=CATALOG_VERSION,
        )
    with pytest.raises(ValueError, match="date source"):
        DeclaredActivityFieldDefinition(
            field_name="x",
            family="insider",
            applicable_forms=("4",),
            source_id=OWNERSHIP_SOURCE_ID,
            source_attribute="entry.shares",
            unit="shares",
            frequency=DataFrequency.EVENT,
            date_sources=(),
            quality=DataQuality.VALID,
            limitations="invalid",
            catalog_version=CATALOG_VERSION,
        )


def test_field_definition_rejects_malformed_source_attribute() -> None:
    with pytest.raises(ValueError, match="entry.<name>"):
        DeclaredActivityFieldDefinition(
            field_name="x",
            family="insider",
            applicable_forms=("4",),
            source_id=OWNERSHIP_SOURCE_ID,
            source_attribute="shares",
            unit="shares",
            frequency=DataFrequency.EVENT,
            date_sources=(
                DeclaredActivityDateSource(
                    attribute="entry.transaction_date", target="observed_at"
                ),
            ),
            quality=DataQuality.VALID,
            limitations="invalid",
            catalog_version=CATALOG_VERSION,
        )


def test_date_source_rejects_malformed_attribute() -> None:
    with pytest.raises(ValueError, match="entry.<name>"):
        DeclaredActivityDateSource(attribute="transaction_date", target="observed_at")
    with pytest.raises(ValueError, match="entry.<name>"):
        DeclaredActivityDateSource(attribute="bogus.transaction_date", target="observed_at")
