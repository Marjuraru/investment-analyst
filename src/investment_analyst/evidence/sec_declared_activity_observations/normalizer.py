"""Pure, deterministic projection of declared-activity statements to observations.

No clock, no storage, no network. Every function here is a total, side-effect-free
transformation of already-persisted evidence into zero or more ``NormalizedObservation``
plus an explicit accounting of every value that could not be normalized and why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.core.models.source import SourceReference
from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipStatement
from investment_analyst.evidence.sec_declared_activity_observations.definitions import (
    TRANSFORMATION_VERSION,
    DeclaredActivityDateSource,
    DeclaredActivityFamily,
    DeclaredActivityFieldDefinition,
    get_field_definitions_for_family,
)
from investment_analyst.evidence.sec_ownership.models import OwnershipEntry, OwnershipStatement

_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst:sec-declared-activity-observation:v1")


class DeclaredActivityNormalizationError(ValueError):
    """Raised when a statement cannot be normalized under a fixed clock."""


@dataclass(frozen=True, slots=True)
class SkippedDeclaredActivityValue:
    """One declared value that could not become a NormalizedObservation, with its reason."""

    family: DeclaredActivityFamily
    statement_id: UUID
    entry_id: UUID | None
    field_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class DeclaredActivityNormalizationResult:
    """Pure output of normalizing one statement: generated plus skipped values."""

    observations: tuple[NormalizedObservation, ...] = field(default_factory=tuple)
    skipped: tuple[SkippedDeclaredActivityValue, ...] = field(default_factory=tuple)


def expected_observation_id(
    *,
    source_id: str,
    statement_id: UUID,
    entry_id: UUID | None,
    field_name: str,
    transformation_version: str = TRANSFORMATION_VERSION,
) -> UUID:
    """Deterministic identity excluding the normalization clock, value, or snapshot."""
    entry_component = str(entry_id) if entry_id is not None else "-"
    return uuid5(
        _NAMESPACE,
        f"{source_id}|{statement_id}|{entry_component}|{field_name}|{transformation_version}",
    )


def normalize_ownership_statement(
    statement: OwnershipStatement,
    *,
    normalized_at: datetime,
) -> DeclaredActivityNormalizationResult:
    """Project one Form 3/4/5 statement's declared entries to observations."""
    _require_normalized_at_not_before_available(statement.available_at, normalized_at)
    observations: list[NormalizedObservation] = []
    skipped: list[SkippedDeclaredActivityValue] = []
    fields = get_field_definitions_for_family("insider")
    for entry in statement.entries:
        for definition in fields:
            _apply_field(
                definition,
                entry=entry,
                statement=statement,
                statement_id=statement.statement_id,
                entry_id=entry.entry_id,
                raw_record_id=statement.raw_record_id,
                asset_id=statement.asset_id,
                available_at=statement.available_at,
                normalized_at=normalized_at,
                retrieved_at=statement.parsed_at,
                raw_uri=statement.document_revision.source_url,
                checksum_sha256=statement.document_revision.content_sha256,
                observations=observations,
                skipped=skipped,
            )
    return DeclaredActivityNormalizationResult(
        observations=tuple(observations), skipped=tuple(skipped)
    )


def normalize_beneficial_ownership_statement(
    statement: BeneficialOwnershipStatement,
    *,
    normalized_at: datetime,
) -> DeclaredActivityNormalizationResult:
    """Project one Schedule 13D/13G statement's declared fields to observations."""
    _require_normalized_at_not_before_available(statement.available_at, normalized_at)
    observations: list[NormalizedObservation] = []
    skipped: list[SkippedDeclaredActivityValue] = []
    for definition in get_field_definitions_for_family("beneficial_ownership"):
        _apply_field(
            definition,
            entry=None,
            statement=statement,
            statement_id=statement.statement_id,
            entry_id=None,
            raw_record_id=statement.raw_record_id,
            asset_id=statement.asset_id,
            available_at=statement.available_at,
            normalized_at=normalized_at,
            retrieved_at=statement.parsed_at,
            raw_uri=statement.document_revision.source_url,
            checksum_sha256=statement.document_revision.content_sha256,
            observations=observations,
            skipped=skipped,
        )
    return DeclaredActivityNormalizationResult(
        observations=tuple(observations), skipped=tuple(skipped)
    )


def _apply_field(
    definition: DeclaredActivityFieldDefinition,
    *,
    entry: OwnershipEntry | None,
    statement: OwnershipStatement | BeneficialOwnershipStatement,
    statement_id: UUID,
    entry_id: UUID | None,
    raw_record_id: UUID,
    asset_id: str,
    available_at: datetime,
    normalized_at: datetime,
    retrieved_at: datetime,
    raw_uri: str,
    checksum_sha256: str,
    observations: list[NormalizedObservation],
    skipped: list[SkippedDeclaredActivityValue],
) -> None:
    value = _read_attribute(entry, statement, definition.source_attribute)
    if value is None:
        skipped.append(
            SkippedDeclaredActivityValue(
                family=definition.family,
                statement_id=statement_id,
                entry_id=entry_id,
                field_name=definition.field_name,
                reason="missing_value",
            )
        )
        return
    if not isinstance(value, Decimal):
        raise DeclaredActivityNormalizationError(
            f"declared value for {definition.field_name} must be Decimal,"
            f" not {type(value).__name__}"
        )
    if not value.is_finite():
        raise DeclaredActivityNormalizationError(
            f"declared value for {definition.field_name} must be finite"
        )
    resolved = _resolve_declared_date(entry, statement, definition.date_sources)
    if resolved is None:
        skipped.append(
            SkippedDeclaredActivityValue(
                family=definition.family,
                statement_id=statement_id,
                entry_id=entry_id,
                field_name=definition.field_name,
                reason="missing_date",
            )
        )
        return
    date_attribute, observed_at, period_end = resolved
    observation_id = expected_observation_id(
        source_id=definition.source_id,
        statement_id=statement_id,
        entry_id=entry_id,
        field_name=definition.field_name,
    )
    record_key = json.dumps(
        {
            "statement_id": str(statement_id),
            "entry_id": str(entry_id) if entry_id is not None else None,
            "field_name": definition.field_name,
            "date_attribute": date_attribute,
            "transformation_version": TRANSFORMATION_VERSION,
        },
        sort_keys=True,
    )
    observations.append(
        NormalizedObservation(
            observation_id=observation_id,
            raw_record_id=raw_record_id,
            asset_id=asset_id,
            field_name=definition.field_name,
            value=value,
            unit=definition.unit,
            frequency=definition.frequency,
            observed_at=observed_at,
            period_end=period_end,
            available_at=available_at,
            normalized_at=normalized_at,
            source=SourceReference(
                source_id=definition.source_id,
                record_key=record_key,
                retrieved_at=retrieved_at,
                raw_uri=raw_uri,
                checksum_sha256=checksum_sha256,
            ),
            quality=definition.quality,
            transformation_version=TRANSFORMATION_VERSION,
        )
    )


def _read_attribute(
    entry: OwnershipEntry | None,
    statement: OwnershipStatement | BeneficialOwnershipStatement,
    dotted: str,
) -> Decimal | None:
    scope, _, name = dotted.partition(".")
    target = entry if scope == "entry" else statement
    if target is None:
        return None
    return getattr(target, name)


def _resolve_declared_date(
    entry: OwnershipEntry | None,
    statement: OwnershipStatement | BeneficialOwnershipStatement,
    date_sources: tuple[DeclaredActivityDateSource, ...],
) -> tuple[str, datetime | None, datetime | None] | None:
    for source in date_sources:
        scope, _, name = source.attribute.partition(".")
        target = entry if scope == "entry" else statement
        if target is None:
            continue
        value: date | None = getattr(target, name)
        if value is None:
            continue
        moment = _date_at_utc_midnight(value)
        if source.target == "observed_at":
            return source.attribute, moment, None
        return source.attribute, None, moment
    return None


def _date_at_utc_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _require_normalized_at_not_before_available(
    available_at: datetime, normalized_at: datetime
) -> None:
    if normalized_at < available_at:
        raise DeclaredActivityNormalizationError(
            "normalized_at must not precede the declared evidence's available_at"
        )
