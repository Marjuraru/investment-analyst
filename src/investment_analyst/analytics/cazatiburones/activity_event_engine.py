"""Pure evaluation and deterministic cooldown projection for activity events."""

from datetime import timedelta
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.activity_event_definitions import (
    COOLDOWN_SECONDS,
    POLICY_VERSION,
    RULES,
)
from investment_analyst.analytics.cazatiburones.activity_event_identity import (
    candidate_id,
    event_id,
)
from investment_analyst.analytics.cazatiburones.activity_event_models import (
    ActivityCandidate,
    ActivityEvaluation,
    ActivityEvent,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult


def project_activity_events(
    metrics: tuple[MetricResult, ...],
) -> tuple[
    tuple[ActivityEvaluation, ...], tuple[ActivityEvent, ...], tuple[ActivityCandidate, ...]
]:
    """Evaluate only valid persisted metrics; no storage, clock, or provider access."""
    evaluations: list[ActivityEvaluation] = []
    events: list[ActivityEvent] = []
    for metric in metrics:
        for rule in (rule for rule in RULES if rule.metric_key == metric.metric_key):
            evaluation = _evaluate(metric, rule.direction, rule.rule_id, rule.unit)
            evaluations.append(evaluation)
            if evaluation.status != "met":
                continue
            payload = {
                "asset_id": metric.asset_id,
                "rule_id": rule.rule_id,
                "metric_key": metric.metric_key,
                "algorithm_version": metric.algorithm_version,
                "unit": metric.unit,
                "as_of": metric.as_of,
                "available_at": metric.available_at,
                "input_observation_ids": metric.input_observation_ids,
                "parameters": metric.parameters,
            }
            identifier = event_id(payload)
            events.append(
                ActivityEvent(
                    event_id=identifier,
                    asset_id=metric.asset_id,
                    rule_id=rule.rule_id,
                    metric_result_id=metric.result_id,
                    metric_key=metric.metric_key,
                    unit=metric.unit,
                    value=metric.value,
                    available_at=metric.available_at,
                    input_observation_ids=tuple(metric.input_observation_ids),
                    parameters=metric.parameters,
                )
            )
    ordered = tuple(
        sorted(
            {item.event_id: item for item in events}.values(),
            key=lambda item: (item.available_at, str(item.event_id)),
        )
    )
    return tuple(evaluations), ordered, _candidates(ordered)


def _evaluate(metric: MetricResult, direction: str, rule_id: str, unit: str) -> ActivityEvaluation:
    if metric.quality is not DataQuality.VALID:
        return ActivityEvaluation(
            rule_id=rule_id,
            metric_result_id=metric.result_id,
            status="not_evaluable",
            reason="quality_not_valid",
            unit=unit,
        )
    met = metric.value > Decimal("0") if direction == "increased" else metric.value < Decimal("0")
    return ActivityEvaluation(
        rule_id=rule_id,
        metric_result_id=metric.result_id,
        status="met" if met else "not_met",
        reason=None if met else "declared_zero_or_opposite_direction",
        value=metric.value if met else None,
        unit=unit,
    )


def _candidates(events: tuple[ActivityEvent, ...]) -> tuple[ActivityCandidate, ...]:
    latest: dict[tuple[str, str, str], ActivityEvent] = {}
    candidates: list[ActivityCandidate] = []
    for event in events:
        group = (event.asset_id, event.rule_id, _participant(event))
        previous = latest.get(group)
        identifier = candidate_id(event.event_id, POLICY_VERSION)
        if previous is not None and event.available_at < previous.available_at + timedelta(
            seconds=COOLDOWN_SECONDS
        ):
            candidates.append(
                ActivityCandidate(
                    candidate_id=identifier,
                    event_id=event.event_id,
                    status="suppressed",
                    cooldown_until=previous.available_at + timedelta(seconds=COOLDOWN_SECONDS),
                    suppressed_by_event_id=previous.event_id,
                )
            )
            continue
        latest[group] = event
        candidates.append(
            ActivityCandidate(candidate_id=identifier, event_id=event.event_id, status="eligible")
        )
    return tuple(candidates)


def _participant(event: ActivityEvent) -> str:
    parameters = event.parameters
    return str(
        parameters.get("participant_cik")
        or parameters.get("reporting_person_cik")
        or "unidentified"
    )
