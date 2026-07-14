"""Prevalidated idempotent persistence for Apple fundamental diagnostics."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from investment_analyst.core.models import DiagnosticMode, DiagnosticResult
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
    SecFundamentalDiagnosticTraceabilityError,
    fundamental_diagnostic_id,
    verify_fundamental_diagnostic_computation,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticRequest,
    SecFundamentalDiagnosticRunSummary,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID
from investment_analyst.storage import LocalStorage, StorageError


class SecFundamentalDiagnosticPipelineError(RuntimeError):
    """Base error for Apple fundamental diagnostic persistence."""


class SecFundamentalDiagnosticIdentityConflictError(SecFundamentalDiagnosticPipelineError):
    """Raised when an existing diagnostic conflicts with its deterministic identity."""


class SecFundamentalDiagnosticPipelineTraceabilityError(SecFundamentalDiagnosticPipelineError):
    """Raised when persistence changes inputs or breaks diagnostic traceability."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecFundamentalDiagnosticPipelineError(f"{name} must include timezone information")
    return value.astimezone(UTC)


def _same_identity(existing: DiagnosticResult, expected: DiagnosticResult) -> bool:
    fields = (
        "diagnostic_id",
        "asset_id",
        "mode",
        "verdict",
        "final_score",
        "confidence",
        "as_of",
        "available_at",
        "components",
        "evidence",
        "algorithm_version",
        "summary",
        "quality",
    )
    return all(getattr(existing, field) == getattr(expected, field) for field in fields)


class SecAaplFundamentalDiagnosticPipeline:
    """Select, compute, prevalidate, and persist one fundamental diagnostic."""

    def __init__(
        self,
        storage: LocalStorage,
        selector: SecFundamentalDiagnosticSelector,
        engine: SecFundamentalDiagnosticEngine,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._selector = selector
        self._engine = engine
        self._clock = clock

    def run(
        self,
        request: SecFundamentalDiagnosticRequest,
    ) -> SecFundamentalDiagnosticRunSummary:
        """Execute one logically prevalidated and idempotent diagnostic run."""
        self._storage.require_open()
        counts_before = self._counts()
        selection = self._selector.select(request)
        computed_at = _utc(self._clock(), "computed_at")
        computation = self._engine.compute(
            request,
            selection,
            computed_at=computed_at,
        )

        try:
            verify_fundamental_diagnostic_computation(computation)
        except SecFundamentalDiagnosticTraceabilityError as error:
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "generated fundamental diagnostic failed pre-persistence validation"
            ) from error
        diagnostic = computation.diagnostic
        if diagnostic.diagnostic_id != fundamental_diagnostic_id(selection):
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "diagnostic identity does not match selected metrics"
            )

        existing_diagnostics = self._storage.diagnostics.list(
            asset_id=ASSET_ID,
            mode=DiagnosticMode.FUNDAMENTAL,
        )
        existing_by_id = {item.diagnostic_id: item for item in existing_diagnostics}
        if len(existing_by_id) != len(existing_diagnostics):
            raise SecFundamentalDiagnosticPipelineError(
                "stored fundamental diagnostic IDs are not unique"
            )
        existing = existing_by_id.get(diagnostic.diagnostic_id)
        if existing is not None and not _same_identity(existing, diagnostic):
            raise SecFundamentalDiagnosticIdentityConflictError(
                "stored diagnostic conflicts with its deterministic identity"
            )

        created = 0
        reused = 0
        if existing is None:
            self._storage.diagnostics.save(diagnostic)
            created = 1
            stored = self._storage.diagnostics.get(diagnostic.diagnostic_id)
        else:
            reused = 1
            stored = existing

        expected_with_stored_time = diagnostic.model_copy(
            update={"computed_at": stored.computed_at}
        )
        if stored != expected_with_stored_time:
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "stored diagnostic does not match the prevalidated result"
            )
        counts_after = self._counts()
        self._verify_counts(counts_before, counts_after, created)

        selected_ids = selection.metric_result_ids()
        referenced_ids = {
            identifier
            for component in stored.components
            for identifier in component.metric_result_ids
        }
        referenced_ids.update(item.metric_result_id for item in stored.evidence)
        if not referenced_ids <= set(selected_ids):
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "stored diagnostic references unselected metric results"
            )
        if stored.components and referenced_ids != set(selected_ids):
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "stored diagnostic does not reference every selected metric"
            )

        return SecFundamentalDiagnosticRunSummary(
            asset_id=ASSET_ID,
            known_at=request.known_at,
            frequency=request.frequency,
            target_period_end=selection.target_period_end,
            computed_at=stored.computed_at,
            diagnostic_id=stored.diagnostic_id,
            selected_metric_result_ids=selected_ids,
            missing_requirements=computation.missing_requirements,
            final_score=stored.final_score,
            verdict=stored.verdict,
            quality=stored.quality,
            confidence=stored.confidence,
            coverage=computation.coverage,
            recency_factor=computation.recency_factor,
            diagnostics_generated=1,
            diagnostics_created=created,
            diagnostics_reused=reused,
            raw_records_created=0,
            observations_created=0,
            metric_results_created=0,
            traceability_verified=True,
        )

    def _counts(self) -> tuple[int, int, int, int]:
        tables = (
            "raw_record_index",
            "normalized_observations",
            "metric_results",
            "diagnostic_results",
        )
        counts: list[int] = []
        try:
            for table in tables:
                row = self._storage.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"  # noqa: S608
                ).fetchone()
                if row is None:
                    raise SecFundamentalDiagnosticPipelineError(
                        f"could not count protected table {table}"
                    )
                counts.append(int(row[0]))
        except StorageError as error:
            raise SecFundamentalDiagnosticPipelineError(
                "storage counts could not be read"
            ) from error
        return counts[0], counts[1], counts[2], counts[3]

    @staticmethod
    def _verify_counts(
        before: tuple[int, int, int, int],
        after: tuple[int, int, int, int],
        created: int,
    ) -> None:
        if after[0] != before[0]:
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "fundamental diagnostic pipeline changed raw records"
            )
        if after[1] != before[1]:
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "fundamental diagnostic pipeline changed observations"
            )
        if after[2] != before[2]:
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "fundamental diagnostic pipeline changed metric results"
            )
        if after[3] != before[3] + created:
            raise SecFundamentalDiagnosticPipelineTraceabilityError(
                "fundamental diagnostic count changed unexpectedly"
            )


def diagnostic_score_matches_components(result: DiagnosticResult) -> bool:
    """Return whether a sufficient diagnostic score matches its weighted components."""
    if not result.components:
        return result.final_score == 0
    total = sum(
        (item.weighted_contribution for item in result.components),
        Decimal("0"),
    )
    return abs(total - result.final_score) <= Decimal("0.0001")
