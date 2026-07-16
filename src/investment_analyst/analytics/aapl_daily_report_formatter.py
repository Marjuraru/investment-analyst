"""Deterministic human-readable formatting for the Apple daily report."""

import json
from datetime import date
from uuid import UUID

from pydantic import JsonValue

from investment_analyst.analytics.aapl_daily_report_models import (
    AaplDailyDiagnosticReport,
    AaplDailyDiagnosticSection,
    AaplDailyMetric,
)
from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedSectionStatus,
)


def format_aapl_daily_report(report: AaplDailyDiagnosticReport) -> str:
    """Render one report without consulting clocks, providers, or mutable state."""
    request = report.view.request
    lines = [
        "APPLE (AAPL) DAILY DIAGNOSTIC REPORT",
        f"Schema: {report.schema_version}",
        f"Known at: {request.known_at.isoformat()}",
        f"Fundamental frequency: {request.fundamental_frequency.value}",
        f"Availability: {report.view.status.value}",
        f"Requested market period: {_requested_period(request.market_as_of)}",
        f"Requested fundamental period: {_requested_period(request.fundamental_as_of)}",
        "",
    ]
    lines.extend(_section_lines("MARKET DIAGNOSTIC", report.market))
    lines.append("")
    lines.extend(_section_lines("FUNDAMENTAL DIAGNOSTIC", report.fundamental))
    lines.extend(
        [
            "",
            "TEMPORAL CONTEXT",
            f"Reference gap days: {_optional(report.view.temporal_context.reference_gap_days)}",
            "",
            "TRACEABILITY",
            f"Diagnostics examined: {report.view.diagnostics_examined}",
            f"Metric results examined: {report.view.metric_results_examined}",
            f"Ignored algorithm versions: {report.view.ignored_algorithm_versions}",
            f"Verified: {str(report.view.traceability_verified).lower()}",
            "",
            "LIMITATIONS",
        ]
    )
    lines.extend(f"- {item}" for item in report.limitations)
    return "\n".join(lines)


def _requested_period(value: date | None) -> str:
    return value.isoformat() if value is not None else "latest eligible"


def _optional(value: int | None) -> str:
    return "not available" if value is None else str(value)


def _section_lines(
    heading: str,
    section: AaplDailyDiagnosticSection,
) -> list[str]:
    selection = section.selection
    lines = [
        heading,
        f"Status: {selection.status.value}",
        f"Candidates: {selection.candidates_eligible} eligible / "
        f"{selection.candidates_examined} examined",
        f"Revisions superseded: {selection.revisions_superseded}",
    ]
    if selection.status is ConsolidatedSectionStatus.NOT_FOUND:
        lines.append(f"Reason: {selection.not_found_reason}")
        return lines

    diagnostic = selection.diagnostic
    if diagnostic is None:
        raise ValueError("available daily report section has no diagnostic")
    lines.extend(
        [
            f"Reference time: {diagnostic.as_of.isoformat()} "
            f"({section.reference_age_days} calendar day(s) before known_at)",
            f"Available at: {diagnostic.available_at.isoformat()} "
            f"({section.availability_age_days} calendar day(s) before known_at)",
            f"Computed at: {diagnostic.computed_at.isoformat()}",
            f"Computed after known_at: {str(selection.computed_after_known_at).lower()}",
            f"Verdict: {diagnostic.verdict.value}",
            f"Score: {diagnostic.final_score}/100",
            f"Confidence: {diagnostic.confidence} "
            "(evidence coverage, not a calibrated probability)",
            f"Quality: {diagnostic.quality.value}",
            f"Algorithm: {diagnostic.algorithm_version}",
            f"Summary: {diagnostic.summary}",
            "Metrics:",
        ]
    )
    if section.metrics:
        for metric in section.metrics:
            lines.extend(_metric_lines(metric))
    else:
        lines.append("- None (diagnostic reports insufficient data).")

    metric_index = {item.result_id: item for item in section.metrics}
    lines.append("Components:")
    if diagnostic.components:
        for component in diagnostic.components:
            names = _metric_names(component.metric_result_ids, metric_index)
            lines.append(
                f"- {component.component_key}: score {component.score}, weight "
                f"{component.weight}, weighted contribution {component.weighted_contribution}; "
                f"metrics: {names}; {component.explanation}"
            )
    else:
        lines.append("- None.")

    lines.append("Evidence:")
    if diagnostic.evidence:
        for evidence in diagnostic.evidence:
            metric_name = _metric_name(evidence.metric_result_id, metric_index)
            lines.append(
                f"- {metric_name}: {evidence.direction.value}, contribution "
                f"{evidence.contribution}; {evidence.reason}"
            )
    else:
        lines.append("- None.")
    return lines


def _metric_lines(metric: AaplDailyMetric) -> list[str]:
    parameters = json.dumps(
        _human_parameters(metric.parameters),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return [
        f"- {metric.display_name} [{metric.metric_key}]: {metric.value} {metric.unit}",
        f"  Formula: {metric.formula}",
        f"  Parameters: {parameters}",
        f"  Reference: {metric.as_of.isoformat()}; available: "
        f"{metric.available_at.isoformat()}; computed: {metric.computed_at.isoformat()}",
        f"  Quality: {metric.quality.value}; algorithm: {metric.algorithm_version}",
        f"  Traceability: result {metric.result_id}; "
        f"{len(metric.input_observation_ids)} input observation(s)",
    ]


def _human_parameters(parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Keep input roles visible in text without repeating every observation UUID."""
    compact = dict(parameters)
    input_roles = compact.get("input_roles")
    if not isinstance(input_roles, list):
        return compact
    roles: list[str] = []
    for item in input_roles:
        if not isinstance(item, dict):
            return compact
        role = item.get("role")
        if not isinstance(role, str):
            return compact
        roles.append(role)
    compact["input_roles"] = roles
    return compact


def _metric_names(
    identifiers: list[UUID],
    metric_index: dict[UUID, AaplDailyMetric],
) -> str:
    return ", ".join(_metric_name(identifier, metric_index) for identifier in identifiers)


def _metric_name(
    identifier: UUID,
    metric_index: dict[UUID, AaplDailyMetric],
) -> str:
    metric = metric_index.get(identifier)
    if metric is None:
        raise ValueError(f"daily report evidence references unresolved metric {identifier}")
    window = metric.parameters.get("window")
    qualifier = (
        f" (window={window})" if isinstance(window, int) and not isinstance(window, bool) else ""
    )
    return f"{metric.display_name}{qualifier} [{metric.metric_key}]"


__all__ = ["format_aapl_daily_report"]
