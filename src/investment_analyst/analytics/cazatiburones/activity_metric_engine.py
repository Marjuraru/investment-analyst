# ruff: noqa: E501
"""Pure point-in-time computation of persistible cazatiburones activity metrics.

Consumes only statements already read from evidence repositories and an in-memory index of
``NormalizedObservation`` already read from layer-2 storage, keyed by the deterministic
``expected_observation_id`` of the field they carry. No clock, no storage, no network: every
function here is a total, side-effect-free transformation of already-read evidence into zero
or more ``ActivityMetricCandidate`` plus an explicit accounting of every pair that could not
produce a metric and why.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Context
from uuid import UUID

from investment_analyst.analytics.cazatiburones.activity_metric_definitions import (
    ALGORITHM_VERSION,
)
from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricCandidate,
    ActivityMetricSkip,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_declared_activity_observations.normalizer import (
    expected_observation_id,
)
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_SOURCE_ID,
    OwnershipEntry,
    OwnershipStatement,
)

RATIO_DECIMAL_PRECISION = 28
_INSIDER_HOLDING_METRIC_KEY = "cazatiburones.insider.holding_delta_ratio"
_BENEFICIAL_METRIC_FIELDS: tuple[tuple[str, str, str], ...] = (
    (
        "cazatiburones.beneficial.delta_percent_of_class",
        "beneficial_percent_of_class",
        "percentage_points",
    ),
    (
        "cazatiburones.beneficial.delta_shares_beneficially_owned",
        "beneficial_shares_owned",
        "shares",
    ),
)

ObservationIndex = Mapping[UUID, NormalizedObservation]


@dataclass(frozen=True, slots=True)
class ActivityMetricEngineResult:
    """Pure output of one engine call: generated candidates plus skipped pairs."""

    candidates: tuple[ActivityMetricCandidate, ...] = field(default_factory=tuple)
    skipped: tuple[ActivityMetricSkip, ...] = field(default_factory=tuple)


def calculate_insider_activity_metrics(
    statements: Iterable[OwnershipStatement],
    *,
    observations: ObservationIndex,
    known_at: datetime,
) -> ActivityMetricEngineResult:
    """Compute the insider holding-delta ratio over consecutive declared holdings."""
    statement_rows = tuple(statements)
    if len({statement.asset_id for statement in statement_rows}) > 1:
        raise ValueError("insider activity metrics require evidence for one asset")
    grouped: dict[tuple[str, str, str], list[tuple[OwnershipStatement, OwnershipEntry]]] = (
        defaultdict(list)
    )
    for statement in statement_rows:
        for entry in statement.entries:
            if entry.kind == "transaction":
                grouped[(entry.owner_cik, entry.security_title, entry.table)].append(
                    (statement, entry)
                )
    candidates: list[ActivityMetricCandidate] = []
    skipped: list[ActivityMetricSkip] = []
    for (owner_cik, security_title, table), rows in sorted(grouped.items()):
        rows.sort(
            key=lambda row: (
                row[1].transaction_date or row[0].period_of_report,
                row[0].available_at,
                str(row[1].entry_id),
            )
        )
        for index, (statement, entry) in enumerate(rows):
            if index == 0:
                skipped.append(
                    ActivityMetricSkip(
                        metric_key=_INSIDER_HOLDING_METRIC_KEY,
                        reason="not_evaluable_no_precedent",
                    )
                )
                continue
            previous_statement, previous_entry = rows[index - 1]
            prior_observation = _lookup(
                observations,
                source_id=OWNERSHIP_SOURCE_ID,
                statement_id=previous_statement.statement_id,
                entry_id=previous_entry.entry_id,
                field_name="insider_shares_owned_following",
            )
            current_observation = _lookup(
                observations,
                source_id=OWNERSHIP_SOURCE_ID,
                statement_id=statement.statement_id,
                entry_id=entry.entry_id,
                field_name="insider_shares_owned_following",
            )
            if prior_observation is None or current_observation is None:
                skipped.append(
                    ActivityMetricSkip(
                        metric_key=_INSIDER_HOLDING_METRIC_KEY,
                        reason="missing_input_observation",
                    )
                )
                continue
            prior_value = prior_observation.value
            if prior_value == 0:
                skipped.append(
                    ActivityMetricSkip(
                        metric_key=_INSIDER_HOLDING_METRIC_KEY,
                        reason="not_evaluable_zero_prior",
                    )
                )
                continue
            ratio_context = Context(prec=RATIO_DECIMAL_PRECISION)
            ratio = ratio_context.divide(current_observation.value - prior_value, prior_value)
            candidates.append(
                ActivityMetricCandidate(
                    asset_id=statement.asset_id,
                    metric_key=_INSIDER_HOLDING_METRIC_KEY,
                    value=ratio,
                    unit="ratio",
                    as_of=_as_of(current_observation),
                    available_at=max(
                        prior_observation.available_at, current_observation.available_at
                    ),
                    known_at=known_at,
                    parameters={
                        "family": "insider",
                        "participant_cik": owner_cik,
                        "security_title": security_title,
                        "table": table,
                        "form": statement.form,
                        "declared_date_attribute": _date_attribute(current_observation),
                        "decimal_precision": RATIO_DECIMAL_PRECISION,
                    },
                    input_observation_ids=(
                        prior_observation.observation_id,
                        current_observation.observation_id,
                    ),
                    algorithm_version=ALGORITHM_VERSION,
                    quality=_combined_quality(prior_observation, current_observation),
                )
            )
    return ActivityMetricEngineResult(candidates=tuple(candidates), skipped=tuple(skipped))


def calculate_beneficial_activity_metrics(
    statements: Iterable[BeneficialOwnershipStatement],
    *,
    observations: ObservationIndex,
    known_at: datetime,
) -> ActivityMetricEngineResult:
    """Compute beneficial-ownership percent-of-class and shares deltas."""
    statement_rows = tuple(statements)
    if len({statement.asset_id for statement in statement_rows}) > 1:
        raise ValueError("beneficial activity metrics require evidence for one asset")
    grouped: dict[tuple[str, str], list[BeneficialOwnershipStatement]] = defaultdict(list)
    for statement in statement_rows:
        if statement.reporting_person_cik is not None:
            grouped[(statement.subject_cik, statement.reporting_person_cik)].append(statement)
    candidates: list[ActivityMetricCandidate] = []
    skipped: list[ActivityMetricSkip] = []
    for (subject_cik, reporting_person_cik), rows in sorted(grouped.items()):
        rows.sort(
            key=lambda item: (
                item.event_date or item.available_at.date(),
                item.available_at,
                str(item.statement_id),
            )
        )
        for index, current in enumerate(rows):
            if index == 0:
                for metric_key, _field_name, _unit in _BENEFICIAL_METRIC_FIELDS:
                    skipped.append(
                        ActivityMetricSkip(
                            metric_key=metric_key, reason="not_evaluable_no_precedent"
                        )
                    )
                continue
            previous = rows[index - 1]
            for metric_key, field_name, unit in _BENEFICIAL_METRIC_FIELDS:
                prior_observation = _lookup(
                    observations,
                    source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
                    statement_id=previous.statement_id,
                    entry_id=None,
                    field_name=field_name,
                )
                current_observation = _lookup(
                    observations,
                    source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
                    statement_id=current.statement_id,
                    entry_id=None,
                    field_name=field_name,
                )
                if prior_observation is None or current_observation is None:
                    skipped.append(
                        ActivityMetricSkip(
                            metric_key=metric_key, reason="missing_input_observation"
                        )
                    )
                    continue
                candidates.append(
                    ActivityMetricCandidate(
                        asset_id=current.asset_id,
                        metric_key=metric_key,
                        value=current_observation.value - prior_observation.value,
                        unit=unit,
                        as_of=_as_of(current_observation),
                        available_at=max(
                            prior_observation.available_at, current_observation.available_at
                        ),
                        known_at=known_at,
                        parameters={
                            "family": "beneficial_ownership",
                            "subject_cik": subject_cik,
                            "reporting_person_cik": reporting_person_cik,
                            "form": current.form,
                            "declared_date_attribute": _date_attribute(current_observation),
                        },
                        input_observation_ids=(
                            prior_observation.observation_id,
                            current_observation.observation_id,
                        ),
                        algorithm_version=ALGORITHM_VERSION,
                        quality=_combined_quality(prior_observation, current_observation),
                    )
                )
    return ActivityMetricEngineResult(candidates=tuple(candidates), skipped=tuple(skipped))


def _lookup(
    observations: ObservationIndex,
    *,
    source_id: str,
    statement_id: UUID,
    entry_id: UUID | None,
    field_name: str,
) -> NormalizedObservation | None:
    observation_id = expected_observation_id(
        source_id=source_id,
        statement_id=statement_id,
        entry_id=entry_id,
        field_name=field_name,
    )
    return observations.get(observation_id)


def _as_of(observation: NormalizedObservation) -> datetime:
    resolved = (
        observation.observed_at if observation.observed_at is not None else observation.period_end
    )
    if resolved is None:  # pragma: no cover - excluded by NormalizedObservation's own invariant
        raise ValueError("normalized observation has neither observed_at nor period_end")
    return resolved.astimezone(UTC)


def _date_attribute(observation: NormalizedObservation) -> str:
    return json.loads(observation.source.record_key)["date_attribute"]


def _combined_quality(prior: NormalizedObservation, current: NormalizedObservation) -> DataQuality:
    return current.quality if current.quality == prior.quality else DataQuality.PARTIAL
