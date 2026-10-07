"""Provider-independent descriptive reads from persisted analytical artifacts."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime
from uuid import UUID

from investment_analyst.analytics.analysis_domain import (
    require_authorized_domain,
    validate_diagnostic_mode_for_domain,
    validate_metric_key_for_domain,
)
from investment_analyst.analytics.analysis_snapshot import AnalysisSnapshot
from investment_analyst.analytics.analytical_access_models import (
    DiagnosticAccessRecord,
    DiagnosticComponentAccess,
    DiagnosticEvidenceAccess,
    ExplanationDocument,
    FeatureObservation,
    FeatureSetResult,
    FeatureSetSpec,
    FeatureValue,
    MetricExplanation,
    MetricIndexEntry,
    MetricPageCursor,
    MetricSeriesPage,
    MetricSeriesPoint,
    MetricSeriesQuery,
    SnapshotEvidence,
    analytical_content_hash,
)
from investment_analyst.core.interfaces.analytical_access import AnalyticalAccessPort
from investment_analyst.core.interfaces.repositories import (
    DiagnosticResultRepository,
    MetricDefinitionRepository,
    MetricResultRepository,
)
from investment_analyst.core.models import DiagnosticResult, MetricDefinition, MetricResult
from investment_analyst.storage.errors import RecordNotFoundError, StorageError

_MAX_BATCH = 256


class AnalyticalAccessError(StorageError):
    """A requested persisted projection is missing, ambiguous, or out of scope."""


class RepositoryAnalyticalAccessAdapter:
    """Repository-backed port shared by legacy and compact workspace adapters."""

    def __init__(
        self,
        metric_results: MetricResultRepository,
        diagnostics: DiagnosticResultRepository,
        metric_definitions: MetricDefinitionRepository,
        *,
        snapshot_loader: Callable[[UUID], AnalysisSnapshot] | None = None,
    ) -> None:
        self._metric_results = metric_results
        self._diagnostics = diagnostics
        self._metric_definitions = metric_definitions
        self._snapshot_loader = snapshot_loader

    def validate_snapshot(self, snapshot: AnalysisSnapshot) -> None:
        if self._snapshot_loader is not None:
            persisted = self._snapshot_loader(snapshot.snapshot_id)
            if persisted != snapshot:
                raise AnalyticalAccessError("snapshot differs from its persisted artifact")
            return
        require_authorized_domain(snapshot.domain)

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]:
        return self._metric_results.list_metric_index_page(query)

    def get_metric_results(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        ordered = tuple(sorted(set(result_ids), key=str))
        output: dict[UUID, MetricResult] = {}
        for start in range(0, len(ordered), _MAX_BATCH):
            chunk = ordered[start : start + _MAX_BATCH]
            output.update(self._metric_results.get_many(chunk))
        return output

    def get_diagnostic_results(
        self, diagnostic_ids: Collection[UUID]
    ) -> dict[UUID, DiagnosticResult]:
        ordered = tuple(sorted(set(diagnostic_ids), key=str))
        output: dict[UUID, DiagnosticResult] = {}
        for start in range(0, len(ordered), _MAX_BATCH):
            chunk = ordered[start : start + _MAX_BATCH]
            output.update(self._diagnostics.get_many(chunk))
        return output

    def get_metric_definition(self, metric_key: str) -> MetricDefinition:
        return self._metric_definitions.get(metric_key)

    @staticmethod
    def _validate_diagnostic(
        snapshot: AnalysisSnapshot, domain: str, diagnostic: DiagnosticResult
    ) -> None:
        validate_diagnostic_mode_for_domain(diagnostic.mode, domain)
        if diagnostic.asset_id != snapshot.asset_id or diagnostic.available_at > snapshot.known_at:
            raise AnalyticalAccessError("snapshot references a foreign or future diagnostic")


class AnalyticalAccessService:
    """Read and explain persisted facts without provider access or calculation engines."""

    def __init__(self, port: AnalyticalAccessPort) -> None:
        self._port = port

    def metric_series(self, query: MetricSeriesQuery) -> MetricSeriesPage:
        if not query.metric_keys:
            return self._series_page(query, (), None)
        eligible_entries: list[MetricIndexEntry] = []
        seen_index_ids: set[UUID] = set()
        cursor = query.after
        while len(eligible_entries) < query.limit:
            page_query = query.model_copy(update={"after": cursor})
            index_entries = self._port.list_metric_index_page(page_query)
            if len(index_entries) > query.limit:
                raise AnalyticalAccessError("metric index adapter exceeded the requested page size")
            visible_entries = self._validate_index_entries(page_query, index_entries)
            for entry in index_entries:
                if entry.result_id in seen_index_ids:
                    raise AnalyticalAccessError("metric index adapter repeated a page result")
                seen_index_ids.add(entry.result_id)
            eligible_entries.extend(visible_entries[: query.limit - len(eligible_entries)])
            if len(eligible_entries) >= query.limit or len(index_entries) < query.limit:
                break
            next_cursor = MetricPageCursor(
                available_at=index_entries[-1].available_at,
                result_id=index_entries[-1].result_id,
            )
            if cursor == next_cursor:
                raise AnalyticalAccessError("metric index adapter did not advance its cursor")
            cursor = next_cursor

        if not eligible_entries:
            return self._series_page(query, (), None)

        identifiers = tuple(item.result_id for item in eligible_entries)
        results = self._port.get_metric_results(identifiers)
        if set(results) != set(identifiers):
            raise RecordNotFoundError("metric index page references a missing metric result")
        points: list[MetricSeriesPoint] = []
        for entry in eligible_entries:
            result = results[entry.result_id]
            self._validate_result_against_index(query, entry, result)
            points.append(_metric_series_point(result))
        cursor = (
            MetricPageCursor(
                available_at=eligible_entries[-1].available_at,
                result_id=eligible_entries[-1].result_id,
            )
            if len(eligible_entries) == query.limit
            else None
        )
        return self._series_page(query, tuple(points), cursor)

    def evidence(
        self,
        snapshot: AnalysisSnapshot,
        *,
        metric_result_ids: Collection[UUID] = (),
        diagnostic_ids: Collection[UUID] = (),
    ) -> SnapshotEvidence:
        self._port.validate_snapshot(snapshot)
        selected_metrics = set(metric_result_ids)
        selected_diagnostics = set(diagnostic_ids)
        if not selected_metrics.issubset(snapshot.metric_ids):
            raise AnalyticalAccessError("evidence requested a metric outside the snapshot")
        if not selected_diagnostics.issubset(snapshot.diagnostic_ids):
            raise AnalyticalAccessError("evidence requested a diagnostic outside the snapshot")
        if len(selected_metrics) > _MAX_BATCH or len(selected_diagnostics) > _MAX_BATCH:
            raise AnalyticalAccessError("one evidence query is limited to 256 direct references")

        diagnostics = self._port.get_diagnostic_results(selected_diagnostics)
        if set(diagnostics) != selected_diagnostics:
            raise RecordNotFoundError("snapshot references a missing diagnostic")
        metric_ids = set(selected_metrics)
        for diagnostic in diagnostics.values():
            RepositoryAnalyticalAccessAdapter._validate_diagnostic(
                snapshot, snapshot.domain, diagnostic
            )
            metric_ids.update(_diagnostic_metric_ids(diagnostic))
        if len(metric_ids) > _MAX_BATCH:
            raise AnalyticalAccessError("diagnostic evidence references more than 256 metrics")
        metrics = self._port.get_metric_results(metric_ids)
        if set(metrics) != metric_ids:
            raise RecordNotFoundError("snapshot evidence references a missing metric")
        for metric in metrics.values():
            _validate_metric_for_snapshot(snapshot, snapshot.domain, metric)

        metric_points = tuple(
            _metric_series_point(metrics[identifier])
            for identifier in sorted(
                metric_ids, key=lambda item: (metrics[item].available_at, str(item))
            )
        )
        diagnostic_records = tuple(
            _diagnostic_access_record(diagnostics[identifier])
            for identifier in sorted(diagnostics, key=str)
        )
        content = {
            "snapshot_id": snapshot.snapshot_id,
            "asset_id": snapshot.asset_id,
            "domain": snapshot.domain,
            "known_at": snapshot.known_at,
            "metrics": metric_points,
            "diagnostics": diagnostic_records,
        }
        return SnapshotEvidence(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.asset_id,
            domain=snapshot.domain,
            known_at=snapshot.known_at,
            metrics=metric_points,
            diagnostics=diagnostic_records,
            content_hash=analytical_content_hash(content),
        )

    def features(
        self,
        snapshot: AnalysisSnapshot,
        feature_set: FeatureSetSpec,
    ) -> FeatureSetResult:
        self._validate_feature_set(snapshot, feature_set)
        self._port.validate_snapshot(snapshot)
        selected_ids = self._snapshot_feature_metric_ids(snapshot, feature_set)
        metrics = self._port.get_metric_results(selected_ids)
        if set(metrics) != set(selected_ids):
            raise RecordNotFoundError("snapshot references a missing metric")
        for metric in metrics.values():
            _validate_metric_for_snapshot(snapshot, feature_set.domain, metric)

        values: list[FeatureValue] = []
        for key in feature_set.metric_keys:
            candidates = sorted(
                (item for item in metrics.values() if item.metric_key == key),
                key=lambda item: (item.as_of, item.available_at, str(item.result_id)),
            )
            if not candidates:
                values.append(
                    FeatureValue(
                        metric_key=key,
                        status="missing",
                        values=(),
                        reason="metric_not_in_snapshot",
                    )
                )
                continue
            values.append(
                FeatureValue(
                    metric_key=key,
                    status="available",
                    values=tuple(_feature_observation(item) for item in candidates),
                )
            )

        schema_hash = analytical_content_hash(
            {
                "schema_version": "analytical-feature-set-v1",
                "feature_set": feature_set,
                "result_schema": FeatureSetResult.model_json_schema(),
            }
        )
        content_document = {
            "schema_hash": schema_hash,
            "snapshot_id": snapshot.snapshot_id,
            "feature_set_id": feature_set.feature_set_id,
            "feature_set_version": feature_set.version,
            "domain": feature_set.domain,
            "values": tuple(values),
        }
        return FeatureSetResult(
            snapshot_id=snapshot.snapshot_id,
            feature_set_id=feature_set.feature_set_id,
            feature_set_version=feature_set.version,
            domain=feature_set.domain,
            values=tuple(values),
            schema_hash=schema_hash,
            content_hash=analytical_content_hash(content_document),
        )

    def explain(
        self,
        snapshot: AnalysisSnapshot,
        metric_result_ids: Collection[UUID],
    ) -> ExplanationDocument:
        self._port.validate_snapshot(snapshot)
        requested = tuple(sorted(set(metric_result_ids), key=str))
        if not set(requested).issubset(snapshot.metric_ids):
            raise AnalyticalAccessError("explanation requested a metric outside the snapshot")
        metrics = self._port.get_metric_results(requested)
        if set(metrics) != set(requested):
            raise RecordNotFoundError("snapshot references a missing metric")
        explanations: list[MetricExplanation] = []
        for identifier in requested:
            metric = metrics[identifier]
            _validate_metric_for_snapshot(snapshot, snapshot.domain, metric)
            definition = self._port.get_metric_definition(metric.metric_key)
            if definition.metric_key != metric.metric_key:
                raise AnalyticalAccessError("metric definition key does not match its result")
            parameters = {
                key: value
                for key, value in metric.parameters.items()
                if key not in {"known_at", "computed_at"}
            }
            explanations.append(
                MetricExplanation(
                    result_id=identifier,
                    metric_key=metric.metric_key,
                    value=metric.value,
                    unit=metric.unit,
                    as_of=metric.as_of,
                    available_at=metric.available_at,
                    parameters=parameters,
                    input_observation_ids=tuple(metric.input_observation_ids),
                    input_metric_result_ids=tuple(metric.input_metric_result_ids),
                    algorithm_version=metric.algorithm_version,
                    quality=metric.quality,
                    display_name=definition.display_name,
                    description=definition.description,
                    formula=definition.formula,
                    definition_version=definition.definition_version,
                    limitations=tuple(definition.limitations),
                    references=tuple(definition.references),
                )
            )
        document = {
            "snapshot_id": snapshot.snapshot_id,
            "domain": snapshot.domain,
            "known_at": snapshot.known_at,
            "items": tuple(explanations),
        }
        return ExplanationDocument(
            snapshot_id=snapshot.snapshot_id,
            domain=snapshot.domain,
            known_at=snapshot.known_at,
            items=tuple(explanations),
            content_hash=analytical_content_hash(document),
        )

    @staticmethod
    def _validate_index_entries(
        query: MetricSeriesQuery, entries: tuple[MetricIndexEntry, ...]
    ) -> tuple[MetricIndexEntry, ...]:
        previous: tuple[object, str] | None = None
        visible: list[MetricIndexEntry] = []
        for item in entries:
            if item.asset_id != query.asset_id or item.metric_key not in query.metric_keys:
                raise AnalyticalAccessError(
                    "metric index returned a result outside the query scope"
                )
            validate_metric_key_for_domain(item.metric_key, query.domain)
            if item.available_at > query.known_at:
                raise AnalyticalAccessError("metric index returned a future result")
            if item.legacy_known_at is not None:
                try:
                    legacy_cut = datetime.fromisoformat(item.legacy_known_at)
                except ValueError as error:
                    raise AnalyticalAccessError(
                        "metric index contains a malformed legacy cut"
                    ) from error
                if legacy_cut.tzinfo is None or legacy_cut.utcoffset() is None:
                    raise AnalyticalAccessError("metric index contains a timezone-naive legacy cut")
                legacy_visible = legacy_cut <= query.known_at
            else:
                legacy_visible = True
            if query.as_of_from is not None and item.as_of < query.as_of_from:
                raise AnalyticalAccessError(
                    "metric index returned a result before the requested range"
                )
            if query.as_of_before is not None and item.as_of >= query.as_of_before:
                raise AnalyticalAccessError(
                    "metric index returned a result after the requested range"
                )
            if query.source_id is not None and item.source_id != query.source_id:
                raise AnalyticalAccessError("metric index returned a result from another source")
            if query.frequency is not None and item.frequency != query.frequency:
                raise AnalyticalAccessError("metric index returned a result at another frequency")
            key = (item.available_at, str(item.result_id))
            if previous is not None and key <= previous:
                raise AnalyticalAccessError("metric index page is not in stable keyset order")
            if query.after is not None and key <= (
                query.after.available_at,
                str(query.after.result_id),
            ):
                raise AnalyticalAccessError("metric index page did not advance its cursor")
            previous = key
            if legacy_visible:
                visible.append(item)
        return tuple(visible)

    @staticmethod
    def _validate_result_against_index(
        query: MetricSeriesQuery,
        entry: MetricIndexEntry,
        result: MetricResult,
    ) -> None:
        if (
            result.result_id != entry.result_id
            or result.asset_id != entry.asset_id
            or result.metric_key != entry.metric_key
            or result.as_of != entry.as_of
            or result.available_at != entry.available_at
            or result.parameters.get("source_id") != entry.source_id
            or result.parameters.get("frequency") != entry.frequency
            or result.parameters.get("known_at") != entry.legacy_known_at
        ):
            raise AnalyticalAccessError("metric index projection changed during hydration")
        if result.available_at > query.known_at:
            raise AnalyticalAccessError("metric result is not available at the requested cut")

    def _series_page(
        self,
        query: MetricSeriesQuery,
        points: tuple[MetricSeriesPoint, ...],
        cursor: MetricPageCursor | None,
    ) -> MetricSeriesPage:
        truncated = cursor is not None
        digest = analytical_content_hash(
            {
                "schema_version": "metric-series-page-v1",
                "query": query,
                "items": points,
                "next_cursor": cursor,
                "truncated": truncated,
            }
        )
        return MetricSeriesPage(
            items=points,
            next_cursor=cursor,
            truncated=truncated,
            content_hash=digest,
        )

    @staticmethod
    def _validate_feature_set(snapshot: AnalysisSnapshot, feature_set: FeatureSetSpec) -> None:
        if snapshot.domain != feature_set.domain:
            raise AnalyticalAccessError("feature set and snapshot domains differ")

    def _snapshot_feature_metric_ids(
        self,
        snapshot: AnalysisSnapshot,
        feature_set: FeatureSetSpec,
    ) -> tuple[UUID, ...]:
        snapshot_ids = set(snapshot.metric_ids)
        selected: set[UUID] = set()
        seen_index_ids: set[UUID] = set()
        cursor: MetricPageCursor | None = None
        while True:
            query = MetricSeriesQuery(
                asset_id=snapshot.asset_id,
                domain=snapshot.domain,
                known_at=snapshot.known_at,
                metric_keys=feature_set.metric_keys,
                after=cursor,
                limit=_MAX_BATCH,
            )
            entries = self._port.list_metric_index_page(query)
            if len(entries) > _MAX_BATCH:
                raise AnalyticalAccessError("metric index adapter exceeded the bounded page size")
            visible_entries = self._validate_index_entries(query, entries)
            repeated = seen_index_ids.intersection(item.result_id for item in entries)
            if repeated:
                raise AnalyticalAccessError("metric index adapter repeated a prior page result")
            seen_index_ids.update(item.result_id for item in entries)
            selected.update(
                item.result_id for item in visible_entries if item.result_id in snapshot_ids
            )
            if len(entries) < _MAX_BATCH:
                break
            next_cursor = MetricPageCursor(
                available_at=entries[-1].available_at,
                result_id=entries[-1].result_id,
            )
            if cursor == next_cursor:
                raise AnalyticalAccessError("metric index adapter did not advance its cursor")
            cursor = next_cursor
        return tuple(sorted(selected, key=str))


def _metric_series_point(metric: MetricResult) -> MetricSeriesPoint:
    return MetricSeriesPoint(
        result_id=metric.result_id,
        asset_id=metric.asset_id,
        metric_key=metric.metric_key,
        value=metric.value,
        unit=metric.unit,
        as_of=metric.as_of,
        available_at=metric.available_at,
        parameters={
            key: value
            for key, value in metric.parameters.items()
            if key not in {"known_at", "computed_at"}
        },
        input_observation_ids=tuple(metric.input_observation_ids),
        input_metric_result_ids=tuple(metric.input_metric_result_ids),
        algorithm_version=metric.algorithm_version,
        quality=metric.quality,
    )


def _feature_observation(metric: MetricResult) -> FeatureObservation:
    return FeatureObservation(
        result_id=metric.result_id,
        value=metric.value,
        unit=metric.unit,
        as_of=metric.as_of,
        available_at=metric.available_at,
        input_observation_ids=tuple(metric.input_observation_ids),
        input_metric_result_ids=tuple(metric.input_metric_result_ids),
        algorithm_version=metric.algorithm_version,
        quality=metric.quality,
    )


def _diagnostic_metric_ids(diagnostic: DiagnosticResult) -> set[UUID]:
    identifiers = {
        metric_id
        for component in diagnostic.components
        for metric_id in component.metric_result_ids
    }
    identifiers.update(item.metric_result_id for item in diagnostic.evidence)
    return identifiers


def _diagnostic_access_record(diagnostic: DiagnosticResult) -> DiagnosticAccessRecord:
    return DiagnosticAccessRecord(
        diagnostic_id=diagnostic.diagnostic_id,
        asset_id=diagnostic.asset_id,
        mode=diagnostic.mode,
        verdict=diagnostic.verdict,
        final_score=diagnostic.final_score,
        confidence=diagnostic.confidence,
        as_of=diagnostic.as_of,
        available_at=diagnostic.available_at,
        components=tuple(
            DiagnosticComponentAccess(
                component_key=item.component_key,
                score=item.score,
                weight=item.weight,
                weighted_contribution=item.weighted_contribution,
                metric_result_ids=tuple(item.metric_result_ids),
                explanation=item.explanation,
            )
            for item in diagnostic.components
        ),
        evidence=tuple(
            DiagnosticEvidenceAccess(
                metric_result_id=item.metric_result_id,
                direction=item.direction,
                contribution=item.contribution,
                reason=item.reason,
            )
            for item in diagnostic.evidence
        ),
        algorithm_version=diagnostic.algorithm_version,
        summary=diagnostic.summary,
        quality=diagnostic.quality,
    )


def _validate_metric_for_snapshot(
    snapshot: AnalysisSnapshot,
    domain: str,
    metric: MetricResult,
) -> None:
    if metric.asset_id != snapshot.asset_id or metric.available_at > snapshot.known_at:
        raise AnalyticalAccessError("snapshot references a foreign or future metric")
    validate_metric_key_for_domain(metric.metric_key, domain)
    if metric.as_of.tzinfo is None or metric.as_of.utcoffset() is None:
        raise AnalyticalAccessError("metric as_of is not timezone-aware")


__all__ = ["AnalyticalAccessError", "AnalyticalAccessService", "RepositoryAnalyticalAccessAdapter"]
