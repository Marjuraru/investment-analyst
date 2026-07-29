"""Scheduler adapter for low-consumption point-in-time analytical screening."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.alerts.analytical_engine import (
    AmbiguousAnalyticalMetricError,
    AnalyticalScreeningEngine,
)
from investment_analyst.alerts.analytical_models import (
    AnalyticalScreeningDomain,
    AnalyticalScreeningRequest,
    AnalyticalScreeningRule,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalMonitorReceipt,
    AnalyticalMonitorReceiptStatus,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDomain,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.core.models import MetricResult
from investment_analyst.workspace.models import WorkspaceAccessMode


class AnalyticalMetricSnapshotSelector:
    """Select one exact common-period snapshot without arbitrary revisions."""

    def select(
        self,
        *,
        rule: AnalyticalScreeningRule,
        metrics: tuple[MetricResult, ...],
        source_id: str,
        known_at: datetime,
    ) -> tuple[MetricResult, ...]:
        """Return at most one compatible metric per rule condition."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValueError("known_at must be timezone-aware")
        known_at = known_at.astimezone(UTC)
        expected_cut = known_at.isoformat()
        by_key: dict[str, tuple[MetricResult, ...]] = {}
        for condition in rule.conditions:
            by_key[condition.metric_key] = tuple(
                item
                for item in metrics
                if item.metric_key == condition.metric_key
                and item.available_at <= known_at
                and item.algorithm_version == condition.algorithm_version
                and item.unit == condition.unit
                and item.quality in condition.accepted_qualities
                and item.parameters.get("source_id") == source_id
                and (
                    rule.domain is AnalyticalScreeningDomain.FUNDAMENTALS
                    or item.parameters.get("known_at") == expected_cut
                )
                and all(
                    item.parameters.get(name) == expected
                    for name, expected in condition.parameter_filters.items()
                )
            )
        periods = {item.as_of for candidates in by_key.values() for item in candidates}
        if not periods:
            return ()
        selected_period = max(periods)
        selected: list[MetricResult] = []
        for condition in rule.conditions:
            candidates = tuple(
                item for item in by_key[condition.metric_key] if item.as_of == selected_period
            )
            if candidates:
                latest_available = max(item.available_at for item in candidates)
                candidates = tuple(
                    item for item in candidates if item.available_at == latest_available
                )
            if len(candidates) > 1:
                raise AmbiguousAnalyticalMetricError(
                    f"{condition.condition_id}: multiple compatible metric revisions exist"
                )
            selected.extend(candidates)
        return tuple(selected)


class AnalyticalScreeningMonitor:
    """Observe durable job attempts and screen only newly persisted evidence."""

    def __init__(
        self,
        store: AnalyticalScreeningStateStore,
        runtime: ApplicationRuntime,
        workspace: Path,
        rules: (
            tuple[AnalyticalScreeningRule, ...] | Callable[[], tuple[AnalyticalScreeningRule, ...]]
        ),
        *,
        engine: AnalyticalScreeningEngine | None = None,
        selector: AnalyticalMetricSnapshotSelector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._workspace = workspace.expanduser().resolve(strict=False)
        if callable(rules):
            self._rules = rules
        else:
            configured = tuple(sorted(rules, key=lambda item: (item.rule_id, item.rule_version)))
            self._rules = lambda: configured
        self._engine = engine or AnalyticalScreeningEngine()
        self._selector = selector or AnalyticalMetricSnapshotSelector()
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self, attempt: ScheduledJobAttempt) -> None:
        """Persist one exactly-once receipt and any deterministic screening results."""
        if attempt.status is ScheduledJobAttemptStatus.RUNNING or attempt.completed_at is None:
            return
        if self._store.contains_attempt(attempt.attempt_id):
            return
        reason = self._skip_reason(attempt)
        if reason is not None:
            self._record_skipped(attempt, reason)
            return
        if attempt.execution is None or attempt.definition.asset_id is None:
            raise ValueError("screenable scheduled attempt is missing execution scope")
        domain = _analytical_domain(attempt.definition.domain)
        if domain is None:
            self._record_skipped(attempt, "unsupported_domain")
            return
        asset = self._runtime.catalog.get(attempt.definition.asset_id)
        rules = tuple(
            item
            for item in sorted(
                self._rules(),
                key=lambda candidate: (
                    candidate.rule_id,
                    candidate.rule_version,
                ),
            )
            if item.domain is domain and asset.asset_class in item.asset_classes
        )
        if not rules:
            self._record_skipped(attempt, "no_compatible_rules")
            return
        if len(attempt.execution.source_ids) != 1:
            self._record_skipped(attempt, "ambiguous_source_scope")
            return
        source_id = attempt.execution.source_ids[0]
        known_at = attempt.execution.effective_known_at
        with self._runtime.open_storage(
            StorageLocationRequest(workspace=self._workspace),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            metrics = tuple(storage.metric_results.list(asset_id=asset.asset_id))
        computed_at = self._normalized_clock()
        if computed_at < known_at:
            computed_at = known_at
        results = tuple(
            self._engine.evaluate(
                AnalyticalScreeningRequest(
                    rule=rule,
                    asset_id=asset.asset_id,
                    asset_class=asset.asset_class,
                    source_id=source_id,
                    known_at=known_at,
                    computed_at=computed_at,
                    metrics=self._selector.select(
                        rule=rule,
                        metrics=metrics,
                        source_id=source_id,
                        known_at=known_at,
                    ),
                )
            )
            for rule in rules
        )
        receipt = AnalyticalMonitorReceipt(
            attempt_id=attempt.attempt_id,
            job_id=attempt.definition.job_id,
            asset_id=asset.asset_id,
            status=AnalyticalMonitorReceiptStatus.SCREENED,
            reason="new_compatible_evidence",
            processed_at=computed_at,
            result_ids=tuple(sorted((item.result_id for item in results), key=str)),
        )
        self._store.record_attempt(receipt, results)

    def reconcile(self, attempts: tuple[ScheduledJobAttempt, ...]) -> None:
        """Replay completed scheduler history without provider or duplicate work."""
        for attempt in attempts:
            if attempt.status is not ScheduledJobAttemptStatus.RUNNING:
                self(attempt)

    def _record_skipped(self, attempt: ScheduledJobAttempt, reason: str) -> None:
        processed_at = attempt.completed_at
        if processed_at is None:
            raise ValueError("completed analytical attempt requires completed_at")
        self._store.record_attempt(
            AnalyticalMonitorReceipt(
                attempt_id=attempt.attempt_id,
                job_id=attempt.definition.job_id,
                asset_id=attempt.definition.asset_id,
                status=AnalyticalMonitorReceiptStatus.SKIPPED,
                reason=reason,
                processed_at=processed_at,
            ),
            (),
        )

    @staticmethod
    def _skip_reason(attempt: ScheduledJobAttempt) -> str | None:
        if attempt.status is not ScheduledJobAttemptStatus.SUCCEEDED:
            return f"attempt_{attempt.status.value}"
        if attempt.execution is None:
            return "missing_execution"
        if not attempt.execution.coverage_complete:
            return "incomplete_coverage"
        if not attempt.execution.evidence_changed:
            return "unchanged_evidence"
        if _analytical_domain(attempt.definition.domain) is None:
            return "unsupported_domain"
        if attempt.definition.asset_id is None:
            return "missing_asset_scope"
        return None

    def _normalized_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("analytical monitor clock must be timezone-aware")
        return value.astimezone(UTC)


def _analytical_domain(domain: ScheduledJobDomain) -> AnalyticalScreeningDomain | None:
    if domain is ScheduledJobDomain.MARKET_DAILY:
        return AnalyticalScreeningDomain.MARKET
    if domain is ScheduledJobDomain.FUNDAMENTALS:
        return AnalyticalScreeningDomain.FUNDAMENTALS
    return None


__all__ = [
    "AnalyticalMetricSnapshotSelector",
    "AnalyticalScreeningMonitor",
]
