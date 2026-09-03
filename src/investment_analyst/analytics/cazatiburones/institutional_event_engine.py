"""Pure functional evaluation and cooldown projection for institutional events."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from investment_analyst.analytics.cazatiburones.institutional_event_definitions import (
    COOLDOWN_SECONDS,
    POLICY_VERSION,
    RULES,
)
from investment_analyst.analytics.cazatiburones.institutional_event_identity import (
    candidate_id,
    event_id,
)
from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalCandidate,
    InstitutionalEvaluation,
    InstitutionalEvent,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult


def project_institutional_events(
    metrics: Sequence[MetricResult],
) -> tuple[
    tuple[InstitutionalEvaluation, ...],
    tuple[InstitutionalEvent, ...],
    tuple[InstitutionalCandidate, ...],
]:
    evaluations: list[InstitutionalEvaluation] = []
    events: list[InstitutionalEvent] = []

    # Map rules by metric_key
    rules_by_key = {}
    for r in RULES:
        rules_by_key.setdefault(r.metric_key, []).append(r)

    # Sort input metrics deterministically by available_at, then result_id
    sorted_metrics = sorted(metrics, key=lambda m: (m.available_at, m.result_id))

    for metric in sorted_metrics:
        matching_rules = rules_by_key.get(metric.metric_key, [])
        for rule in matching_rules:
            if metric.quality != DataQuality.VALID:
                evaluations.append(
                    InstitutionalEvaluation(
                        rule_id=rule.rule_id,
                        metric_result_id=metric.result_id,
                        status="not_evaluable",
                        reason="quality_not_valid",
                        unit=rule.unit,
                    )
                )
                continue

            val = metric.value
            is_met = (rule.direction == "increased" and val > Decimal("0")) or (
                rule.direction == "reduced" and val < Decimal("0")
            )

            if not is_met:
                evaluations.append(
                    InstitutionalEvaluation(
                        rule_id=rule.rule_id,
                        metric_result_id=metric.result_id,
                        status="not_met",
                        reason="declared_zero_or_opposite_direction",
                        unit=rule.unit,
                    )
                )
                continue

            evaluations.append(
                InstitutionalEvaluation(
                    rule_id=rule.rule_id,
                    metric_result_id=metric.result_id,
                    status="met",
                    value=val,
                    unit=rule.unit,
                )
            )

            mgr_cik = str(metric.parameters.get("manager_cik") or "")
            report_period = str(metric.parameters.get("report_period") or "")
            prior_report_period = str(metric.parameters.get("prior_report_period") or "")
            cusip = str(metric.parameters.get("cusip") or "")
            title_of_class = str(metric.parameters.get("title_of_class") or "")
            put_call = metric.parameters.get("put_call")

            event_payload = {
                "algorithm_version": metric.algorithm_version,
                "asset_id": metric.asset_id,
                "available_at": metric.available_at,
                "cusip": cusip,
                "input_observation_ids": [str(x) for x in sorted(metric.input_observation_ids)],
                "manager_cik": mgr_cik,
                "metric_key": metric.metric_key,
                "metric_result_id": str(metric.result_id),
                "parameters": {
                    k: str(v) if v is not None else None
                    for k, v in sorted(metric.parameters.items())
                },
                "prior_report_period": prior_report_period,
                "put_call": put_call,
                "report_period": report_period,
                "rule_id": rule.rule_id,
                "title_of_class": title_of_class,
                "unit": rule.unit,
            }

            ev_id = event_id(event_payload)
            events.append(
                InstitutionalEvent(
                    event_id=ev_id,
                    asset_id=metric.asset_id,
                    manager_cik=mgr_cik,
                    report_period=report_period,
                    prior_report_period=prior_report_period,
                    cusip=cusip,
                    title_of_class=title_of_class,
                    put_call=put_call,
                    rule_id=rule.rule_id,
                    metric_result_id=metric.result_id,
                    metric_key=metric.metric_key,
                    algorithm_version=metric.algorithm_version,
                    unit=rule.unit,
                    value=val,
                    available_at=metric.available_at,
                    input_observation_ids=tuple(metric.input_observation_ids),
                    parameters={
                        k: str(v) if v is not None else None for k, v in metric.parameters.items()
                    },
                )
            )

    # Sort events deterministically by (available_at, event_id)
    sorted_events = sorted(events, key=lambda e: (e.available_at, e.event_id))

    # Project candidates with cooldown grouped by position:
    # (asset_id, rule_id, manager_cik, cusip, title_of_class, put_call)
    candidates: list[InstitutionalCandidate] = []
    last_event_by_group: dict[
        tuple[str, str, str, str, str, str | None], tuple[datetime, UUID]
    ] = {}
    delta_cooldown = timedelta(seconds=COOLDOWN_SECONDS)

    for ev in sorted_events:
        group_key = (
            ev.asset_id,
            ev.rule_id,
            ev.manager_cik,
            ev.cusip,
            ev.title_of_class,
            ev.put_call,
        )
        cand_uuid = candidate_id(ev.event_id, POLICY_VERSION)

        last_info = last_event_by_group.get(group_key)
        if last_info is not None:
            last_available_at, last_event_uuid = last_info
            cooldown_until = last_available_at + delta_cooldown
            if ev.available_at < cooldown_until:
                candidates.append(
                    InstitutionalCandidate(
                        candidate_id=cand_uuid,
                        event_id=ev.event_id,
                        status="suppressed",
                        cooldown_until=cooldown_until,
                        suppressed_by_event_id=last_event_uuid,
                    )
                )
                continue

        candidates.append(
            InstitutionalCandidate(
                candidate_id=cand_uuid,
                event_id=ev.event_id,
                status="eligible",
            )
        )
        last_event_by_group[group_key] = (ev.available_at, ev.event_id)

    return tuple(evaluations), tuple(sorted_events), tuple(candidates)
