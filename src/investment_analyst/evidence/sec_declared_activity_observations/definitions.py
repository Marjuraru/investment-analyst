"""Versioned catalog of declared-activity fields normalizable to observations.

The catalog is the single source of truth for which declared attributes may become a
``NormalizedObservation``. The normalizer emits nothing that is not declared here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
)
from investment_analyst.evidence.sec_documents.models import BENEFICIAL_OWNERSHIP_FORMS
from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_FORMS, OWNERSHIP_SOURCE_ID

CATALOG_VERSION = "sec-declared-activity-observation-v1"
TRANSFORMATION_VERSION = "sec-declared-activity-observation-normalizer-v1"

DeclaredActivityFamily = Literal["insider", "beneficial_ownership"]
DeclaredActivityDateTarget = Literal["observed_at", "period_end"]


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class DeclaredActivityDateSource(_Strict):
    """One candidate declared-date attribute and the observation slot it fills."""

    attribute: NonEmptyStr
    target: DeclaredActivityDateTarget

    @model_validator(mode="after")
    def validate_attribute_shape(self) -> DeclaredActivityDateSource:
        scope, _, name = self.attribute.partition(".")
        if scope not in {"entry", "statement"} or not name:
            raise ValueError("date source attribute must be 'entry.<name>' or 'statement.<name>'")
        return self


class DeclaredActivityFieldDefinition(_Strict):
    """Deterministic description of one normalizable declared-activity field."""

    field_name: NonEmptyStr
    family: DeclaredActivityFamily
    applicable_forms: tuple[NonEmptyStr, ...]
    source_id: NonEmptyStr
    source_attribute: NonEmptyStr
    unit: NonEmptyStr
    frequency: DataFrequency
    date_sources: tuple[DeclaredActivityDateSource, ...]
    quality: DataQuality
    limitations: NonEmptyStr
    catalog_version: NonEmptyStr

    @model_validator(mode="after")
    def validate_field_shape(self) -> DeclaredActivityFieldDefinition:
        if not self.applicable_forms:
            raise ValueError("field definition requires at least one applicable form")
        if not self.date_sources:
            raise ValueError("field definition requires at least one declared date source")
        scope, _, name = self.source_attribute.partition(".")
        if scope not in {"entry", "statement"} or not name:
            raise ValueError("source attribute must be 'entry.<name>' or 'statement.<name>'")
        if self.family == "beneficial_ownership" and scope == "entry":
            raise ValueError("beneficial ownership fields have no entries")
        for source in self.date_sources:
            source_scope = source.attribute.partition(".")[0]
            if self.family == "beneficial_ownership" and source_scope == "entry":
                raise ValueError("beneficial ownership fields have no entries")
        return self


def _insider_field(
    *,
    field_name: str,
    source_attribute: str,
    unit: str,
    limitations: str,
) -> DeclaredActivityFieldDefinition:
    return DeclaredActivityFieldDefinition(
        field_name=field_name,
        family="insider",
        applicable_forms=tuple(sorted(OWNERSHIP_FORMS)),
        source_id=OWNERSHIP_SOURCE_ID,
        source_attribute=source_attribute,
        unit=unit,
        frequency=DataFrequency.EVENT,
        date_sources=(
            DeclaredActivityDateSource(attribute="entry.transaction_date", target="observed_at"),
            DeclaredActivityDateSource(attribute="statement.period_of_report", target="period_end"),
        ),
        quality=DataQuality.VALID,
        limitations=limitations,
        catalog_version=CATALOG_VERSION,
    )


def _beneficial_field(
    *,
    field_name: str,
    source_attribute: str,
    unit: str,
    limitations: str,
) -> DeclaredActivityFieldDefinition:
    return DeclaredActivityFieldDefinition(
        field_name=field_name,
        family="beneficial_ownership",
        applicable_forms=tuple(sorted(BENEFICIAL_OWNERSHIP_FORMS)),
        source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
        source_attribute=source_attribute,
        unit=unit,
        frequency=DataFrequency.EVENT,
        date_sources=(
            DeclaredActivityDateSource(attribute="statement.event_date", target="observed_at"),
        ),
        quality=DataQuality.VALID,
        limitations=limitations,
        catalog_version=CATALOG_VERSION,
    )


FIELD_DEFINITIONS: tuple[DeclaredActivityFieldDefinition, ...] = (
    _insider_field(
        field_name="insider_transaction_shares",
        source_attribute="entry.shares",
        unit="shares",
        limitations=(
            "declared transaction shares only; missing when the entry omits shares; "
            "does not distinguish acquisition from disposition"
        ),
    ),
    _insider_field(
        field_name="insider_transaction_price_per_share",
        source_attribute="entry.price_per_share",
        unit="USD/share",
        limitations="declared price only; missing for entries without a reported price",
    ),
    _insider_field(
        field_name="insider_shares_owned_following",
        source_attribute="entry.shares_owned_following",
        unit="shares",
        limitations="declared post-entry holding only; not a computed running balance",
    ),
    _beneficial_field(
        field_name="beneficial_shares_owned",
        source_attribute="statement.shares_beneficially_owned",
        unit="shares",
        limitations="declared beneficial ownership only; missing when the statement omits it",
    ),
    _beneficial_field(
        field_name="beneficial_percent_of_class",
        source_attribute="statement.percent_of_class",
        unit="percent",
        limitations="declared percent of class only; missing when the statement omits it",
    ),
)

if len({definition.field_name for definition in FIELD_DEFINITIONS}) != len(FIELD_DEFINITIONS):
    raise AssertionError("declared activity field catalog has duplicate field names")

FIELD_DEFINITIONS_BY_NAME: dict[str, DeclaredActivityFieldDefinition] = {
    definition.field_name: definition for definition in FIELD_DEFINITIONS
}

_FIELDS_BY_FAMILY: dict[str, tuple[DeclaredActivityFieldDefinition, ...]] = {
    "insider": tuple(d for d in FIELD_DEFINITIONS if d.family == "insider"),
    "beneficial_ownership": tuple(
        d for d in FIELD_DEFINITIONS if d.family == "beneficial_ownership"
    ),
}


def get_field_definition(field_name: str) -> DeclaredActivityFieldDefinition:
    """Return the catalog entry for one declared field name."""
    try:
        return FIELD_DEFINITIONS_BY_NAME[field_name]
    except KeyError as error:
        raise ValueError(f"declared activity field {field_name!r} is not defined") from error


def get_field_definitions_for_family(
    family: DeclaredActivityFamily,
) -> tuple[DeclaredActivityFieldDefinition, ...]:
    """Return the catalog entries declared for one family, in catalog order."""
    return _FIELDS_BY_FAMILY[family]
