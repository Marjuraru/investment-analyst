"""Offline fidelity, recovery and scale smoke for historical analytical imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

import duckdb

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricCategory,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchive,
)
from investment_analyst.storage.historical_analytical_import import (
    HistoricalAnalyticalImporter,
    HistoricalAnalyticalImportError,
)
from investment_analyst.storage.metric_v2 import recalculate_metric_result_id
from investment_analyst.storage.observation_v2_import import ObservationV2Importer
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer
from investment_analyst.workspace.raw_v2_backup import RawV2StagingBackupService
from investment_analyst.workspace.service import WorkspaceService

_BASE_CUT = datetime(2026, 8, 1, tzinfo=UTC)
_LATER_CUT = _BASE_CUT + timedelta(days=1)
_FAMILIES: tuple[tuple[str, MetricCategory], ...] = (
    ("market.close_1d", MetricCategory.MARKET),
    ("fundamental.revenue", MetricCategory.FUNDAMENTAL),
    ("valuation.pe_ratio", MetricCategory.VALUATION),
    ("crypto.funding_rate", MetricCategory.CRYPTO_DERIVATIVES),
    ("event.institutional_activity", MetricCategory.CAZATIBURONES),
)
_CORE_ASSETS = (
    "equity:us:aapl",
    "equity:us:msft",
    "etf:us:spy",
    "crypto:btc-usd",
    "crypto:eth-usd",
)
_ARCHIVE_TABLES = (
    "historical_metric_results",
    "historical_diagnostic_results",
    "historical_analytical_components",
    "historical_analytical_evidence",
    "historical_analytical_sequences",
    "historical_analytical_sequence_segments",
    "historical_analytical_segments",
    "historical_analytical_segment_members",
)


class SmokeError(RuntimeError):
    """Raised when a scratch profile fails an exact, host-independent gate."""


class QueryTracker:
    """Collect query, page, row and memory evidence by named smoke phase."""

    def __init__(self) -> None:
        self.phase_name = "unclassified"
        self.phases: dict[str, dict[str, object]] = {}
        self.unbounded_document_reads: list[str] = []
        self.document_read_queries = 0
        self.document_json_size_queries = 0
        self.segment_member_selects = 0
        self.segment_member_selects_by_phase: dict[str, int] = {}
        self.segment_member_batch_sizes: list[int] = []
        self.max_query_parameters = 0
        self.max_batch_rows = 0
        self.repository_page_sizes: list[int] = []
        self.repository_model_counts: dict[str, int] = {}
        self.archive_model_counts: dict[str, int] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        previous = self.phase_name
        entry = self.phases.setdefault(name, _empty_phase())
        started = time.perf_counter()
        self.phase_name = name
        try:
            yield
        finally:
            entry["elapsed_seconds"] = float(entry["elapsed_seconds"]) + (
                time.perf_counter() - started
            )
            entry["rss_current_after_bytes"] = _rss_current_bytes()
            entry["rss_high_water_after_bytes"] = _rss_high_water_bytes()
            self.phase_name = previous

    def add(self, name: str, value: int = 1) -> None:
        entry = self.phases.setdefault(self.phase_name, _empty_phase())
        counters = cast(dict[str, int], entry["counters"])
        counters[name] = counters.get(name, 0) + value

    def note_query(
        self,
        role: str,
        query: str,
        parameters: object = None,
        *,
        batch_rows: int | None = None,
    ) -> None:
        entry = self.phases.setdefault(self.phase_name, _empty_phase())
        queries = cast(dict[str, int], entry["queries"])
        queries[role] = queries.get(role, 0) + 1
        parameter_count = len(parameters) if isinstance(parameters, Sequence) else 0
        self.max_query_parameters = max(self.max_query_parameters, parameter_count)
        if batch_rows is not None:
            self.max_batch_rows = max(self.max_batch_rows, batch_rows)
        normalized = " ".join(query.lower().split())
        if "document_json" in normalized and normalized.startswith("select"):
            self.document_read_queries += 1
            size_only_aggregate = "sum(strlen(document_json))" in normalized
            if size_only_aggregate:
                self.document_json_size_queries += 1
            elif " where " not in normalized:
                self.unbounded_document_reads.append(normalized[:240])
        if "historical_analytical_segment_members" in normalized and normalized.startswith(
            "select"
        ):
            self.segment_member_selects += 1
            cursor_parameters = 3 if "segment_id > ?" in normalized else 0
            self.segment_member_batch_sizes.append(max(0, parameter_count - cursor_parameters))
            self.segment_member_selects_by_phase[self.phase_name] = (
                self.segment_member_selects_by_phase.get(self.phase_name, 0) + 1
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "phases": self.phases,
            "max_query_parameters": self.max_query_parameters,
            "max_batch_rows": self.max_batch_rows,
            "max_repository_page_rows": max(self.repository_page_sizes, default=0),
            "repository_page_rows": self.repository_page_sizes,
            "repository_model_counts": self.repository_model_counts,
            "archive_model_counts": self.archive_model_counts,
            "document_json_select_queries": self.document_read_queries,
            "document_json_size_aggregate_queries": self.document_json_size_queries,
            "unbounded_document_json_selects": self.unbounded_document_reads,
            "segment_member_select_queries": self.segment_member_selects,
            "segment_member_select_queries_by_phase": self.segment_member_selects_by_phase,
            "segment_member_batch_sizes": self.segment_member_batch_sizes,
        }

    @contextmanager
    def count_restore_connections(self) -> Iterator[None]:
        original_connect = duckdb.connect

        def counted_connect(
            database: str = ":memory:",
            read_only: bool = False,
            config: object = None,
        ) -> _CountedConnection:
            options: dict[str, object] = {"read_only": read_only}
            if config is not None:
                options["config"] = config
            connection = original_connect(database, **options)
            return _CountedConnection(connection, self, "restore")

        duckdb.connect = counted_connect
        try:
            yield
        finally:
            duckdb.connect = original_connect


class _CountedConnection:
    """DuckDB connection proxy that counts SQL without changing query behavior."""

    def __init__(self, connection: object, tracker: QueryTracker, role: str) -> None:
        self._connection = connection
        self._tracker = tracker
        self._role = role

    def execute(self, query: str, parameters: object = None) -> object:
        self._tracker.note_query(self._role, query, parameters)
        execute = self._connection.execute
        if parameters is None:
            return execute(query)
        return execute(query, parameters)

    def executemany(self, query: str, parameters: Sequence[Sequence[object]]) -> object:
        row_parameters = parameters[0] if parameters else ()
        self._tracker.note_query(self._role, query, row_parameters, batch_rows=len(parameters))
        return self._connection.executemany(query, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def _empty_phase() -> dict[str, object]:
    return {
        "elapsed_seconds": 0.0,
        "queries": {"source": 0, "staging": 0, "restore": 0},
        "counters": {},
        "rss_current_before_bytes": _rss_current_bytes(),
        "rss_current_after_bytes": 0,
        "rss_high_water_before_bytes": _rss_high_water_bytes(),
        "rss_high_water_after_bytes": 0,
    }


def _rss_current_bytes() -> int:
    try:
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return 0


def _rss_high_water_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        return 0
    return 0


def _assets() -> tuple[str, ...]:
    return (*_CORE_ASSETS, *(f"equity:synthetic:{index:02d}" for index in range(1, 33)))


def _uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"data-chassis-39-smoke:{label}")


def _ordered_metric_ids(profile: str, count: int) -> tuple[UUID, ...]:
    return tuple(sorted((_uuid(f"{profile}:metric:{index}") for index in range(count)), key=str))


def _chunks[T](items: Sequence[T], size: int = 256) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        file_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(file_digest)
    return digest.hexdigest()


def _asset_for_index(index: int) -> str:
    return _assets()[index % len(_assets())]


def _seed_source(
    root: Path, profile: str, metric_count: int
) -> tuple[Path, str, str, dict[str, int]]:
    initialized = WorkspaceService().initialize(root)
    storage_paths = StoragePaths.from_root(initialized.paths.storage_root)
    available_at = _BASE_CUT - timedelta(days=3)
    retrieved_at = _BASE_CUT - timedelta(days=2)
    reference = SourceReference(
        source_id="smoke:official-fixture",
        record_key="historical-analytical-smoke",
        retrieved_at=retrieved_at,
    )
    raw_records: list[RawRecord] = []
    observations: list[NormalizedObservation] = []
    observation_by_asset: dict[str, UUID] = {}
    shared_observations: list[UUID] = []
    for index in range(720 + len(_assets()) - 1):
        asset_id = "equity:us:aapl" if index < 720 else _assets()[index - 719]
        raw_id = _uuid(f"{profile}:raw:{index}")
        observation_id = _uuid(f"{profile}:observation:{index}")
        raw = RawRecord(
            record_id=raw_id,
            asset_id=asset_id,
            source=reference.model_copy(update={"record_key": f"{profile}:{index}"}),
            event_time=available_at,
            available_at=available_at,
            received_at=retrieved_at,
            payload={"close": f"{100 + index % 500}.2500", "sequence": index},
            schema_version="historical-analytical-smoke-v1",
        )
        observation = NormalizedObservation(
            observation_id=observation_id,
            raw_record_id=raw_id,
            asset_id=asset_id,
            field_name="close",
            value=Decimal(f"{100 + index % 500}.2500"),
            unit="USD",
            frequency=DataFrequency.DAY_1,
            observed_at=available_at,
            available_at=available_at,
            normalized_at=retrieved_at,
            source=raw.source,
            quality=DataQuality.VALID,
            transformation_version="smoke-v1",
        )
        raw_records.append(raw)
        observations.append(observation)
        observation_by_asset.setdefault(asset_id, observation_id)
        if index < 720:
            shared_observations.append(observation_id)

    preliminary_ids = _ordered_metric_ids(profile, metric_count)
    metrics: list[MetricResult] = []
    for index, result_id in enumerate(preliminary_ids):
        if index < 100:
            asset_id = "equity:us:aapl"
            observation_ids = tuple(shared_observations)
        else:
            asset_id = _asset_for_index(index - 100)
            observation_ids = (observation_by_asset[asset_id],)
        family, _category = _FAMILIES[index % len(_FAMILIES)]
        availability = (
            _BASE_CUT - timedelta(days=1)
            if index < metric_count // 2
            else _BASE_CUT + timedelta(days=1)
        )
        parameters: dict[str, object] = {
            "known_at": availability.isoformat(),
            "run_id": f"{profile}-run-{index:04d}",
            "profile": profile,
        }
        provisional = MetricResult(
            result_id=result_id,
            asset_id=asset_id,
            metric_key=family,
            value=Decimal(f"{index % 1000}.1250"),
            unit="USD",
            as_of=availability - timedelta(hours=1),
            available_at=availability,
            computed_at=_BASE_CUT + timedelta(days=4),
            parameters=parameters,
            input_observation_ids=list(observation_ids),
            algorithm_version="historical-analytical-smoke-v1",
            quality=DataQuality.VALID,
        )
        metrics.append(provisional)

    # Give one result a UUIDv8 identity through the only public identity helper.
    v8_index = metric_count - 2
    v8_metric = metrics[v8_index].model_copy(update={"result_id": UUID(int=0)})
    metrics[v8_index] = v8_metric.model_copy(
        update={"result_id": recalculate_metric_result_id(v8_metric)}
    )
    ordered = sorted(metrics, key=lambda item: (item.available_at, str(item.result_id)))
    grouped_metrics: dict[tuple[datetime, str], list[MetricResult]] = {}
    for metric in ordered:
        grouped_metrics.setdefault((metric.available_at, metric.asset_id), []).append(metric)
    forward_targets: dict[UUID, UUID] = {}
    for group in grouped_metrics.values():
        for position in range(0, len(group) - 1, 17):
            forward_targets[group[position].result_id] = group[position + 1].result_id
    linked_metrics: list[MetricResult] = []
    for metric in ordered:
        target = forward_targets.get(metric.result_id)
        if metric.result_id.version == 8:
            target = None
        dependencies = [target] if target is not None else []
        linked_metrics.append(metric.model_copy(update={"input_metric_result_ids": dependencies}))
    metrics = linked_metrics
    diagnostics: list[DiagnosticResult] = []
    for index, metric in enumerate(metrics):
        mode = DiagnosticMode.MARKET if index % 2 == 0 else DiagnosticMode.FUNDAMENTAL
        score = Decimal(70 + index % 30)
        available = metric.available_at
        component = DiagnosticComponent(
            component_key=f"{mode.value}-component-{index % 5}",
            score=score,
            weight=Decimal("1"),
            weighted_contribution=score,
            metric_result_ids=[metric.result_id],
            explanation="Deterministic smoke component with a cited metric.",
        )
        evidence = DiagnosticEvidence(
            metric_result_id=metric.result_id,
            direction=EvidenceDirection.SUPPORTS,
            contribution=Decimal("1.0000"),
            reason="The cited historical metric supports this fixture diagnostic.",
        )
        diagnostics.append(
            DiagnosticResult(
                diagnostic_id=_uuid(f"{profile}:diagnostic:{index}"),
                asset_id=metric.asset_id,
                mode=mode,
                verdict=DiagnosticVerdict.POSITIVE,
                final_score=score,
                confidence=Decimal("0.7500"),
                as_of=metric.as_of,
                available_at=available,
                computed_at=_BASE_CUT + timedelta(days=4),
                components=[component],
                evidence=[evidence],
                algorithm_version="historical-analytical-smoke-v1",
                summary="Descriptive scratch result for archive fidelity.",
                quality=DataQuality.VALID,
            )
        )

    sequence_requests: list[tuple[str, tuple[UUID, ...]]] = []
    for metric in metrics:
        sequence_requests.append(
            ("metric-input-observation-v1", tuple(metric.input_observation_ids))
        )
        sequence_requests.append(("metric-input-result-v1", tuple(metric.input_metric_result_ids)))
    for diagnostic in diagnostics:
        for component in diagnostic.components:
            sequence_requests.append(
                (
                    "diagnostic-component-metric-result-v1",
                    tuple(component.metric_result_ids),
                )
            )
    unique_sequences = set(sequence_requests)
    segment_requests = [
        (link_type, tuple(identifiers[start : start + 256]))
        for link_type, identifiers in sequence_requests
        for start in range(0, len(identifiers), 256)
    ]
    unique_segments = set(segment_requests)

    with LocalStorage(storage_paths) as writer:
        for page in _chunks(raw_records):
            writer.raw_records.save_many(page)
        for page in _chunks(observations):
            writer.observations.save_many(page)
        for page in _chunks(metrics):
            writer.metric_results.save_many(page)
        for page in _chunks(diagnostics):
            writer.diagnostics.save_many(page)
    return (
        storage_paths.root,
        str(initialized.manifest.workspace_id),
        _tree_digest(initialized.paths.root),
        {
            "sequence_requests": len(sequence_requests),
            "unique_sequences": len(unique_sequences),
            "reused_sequences": len(sequence_requests) - len(unique_sequences),
            "segment_requests": len(segment_requests),
            "unique_segments": len(unique_segments),
            "reused_segments": len(segment_requests) - len(unique_segments),
            "forward_metric_links": len(forward_targets),
        },
    )


def _instrument_source(source: LocalStorage, tracker: QueryTracker) -> None:
    connection = _CountedConnection(source.store.connection, tracker, "source")
    source.store._connection = cast(object, connection)  # type: ignore[assignment]
    for name in (
        "raw_records",
        "observations",
        "metric_results",
        "diagnostics",
    ):
        repository = getattr(source, name)
        if hasattr(repository, "_connection"):
            repository._connection = connection
        get_many_method_name = "get_many"
        original_get_many = getattr(repository, get_many_method_name, None)
        for page_method_name in ("list_import_page", "list_observation_import_page"):
            original_page = getattr(repository, page_method_name, None)
            if not callable(original_page):
                continue

            def page_wrapper(*args: object, _original=original_page, _name=name, **kwargs: object):
                page = _original(*args, **kwargs)
                tracker.repository_page_sizes.append(len(page))
                tracker.add(f"{_name}_pages")
                tracker.add(f"{_name}_page_rows", len(page))
                return page

            setattr(repository, page_method_name, page_wrapper)
        if callable(original_get_many):

            def get_many_wrapper(
                identifiers: Sequence[UUID],
                *,
                _original=original_get_many,
                _name=name,
            ):
                models = _original(identifiers)
                tracker.repository_model_counts[_name] = tracker.repository_model_counts.get(
                    _name, 0
                ) + len(models)
                tracker.add(f"{_name}_models_hydrated", len(models))
                return models

            setattr(repository, get_many_method_name, get_many_wrapper)


def _instrument_staging(staging: RawV2Staging, tracker: QueryTracker) -> None:
    original_factory = staging.historical_analytical_archive

    def factory(*, create: bool = False) -> HistoricalAnalyticalArchive:
        archive = original_factory(create=create)
        original_metrics = archive.get_metrics
        original_diagnostics = archive.get_diagnostics

        def get_metrics(identifiers: Sequence[UUID]) -> dict[UUID, MetricResult]:
            result = original_metrics(identifiers)
            tracker.archive_model_counts["metrics"] = tracker.archive_model_counts.get(
                "metrics", 0
            ) + len(result)
            tracker.add("archive_metric_models_hydrated", len(result))
            return result

        def get_diagnostics(identifiers: Sequence[UUID]) -> dict[UUID, DiagnosticResult]:
            result = original_diagnostics(identifiers)
            tracker.archive_model_counts["diagnostics"] = tracker.archive_model_counts.get(
                "diagnostics", 0
            ) + len(result)
            tracker.add("archive_diagnostic_models_hydrated", len(result))
            return result

        archive.get_metrics = get_metrics  # type: ignore[method-assign]
        archive.get_diagnostics = get_diagnostics  # type: ignore[method-assign]
        return archive

    staging.historical_analytical_archive = factory  # type: ignore[method-assign]


def _directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _logical_varchar_bytes(connection: duckdb.DuckDBPyConnection) -> dict[str, int]:
    output: dict[str, int] = {}
    for table in _ARCHIVE_TABLES:
        columns = connection.execute(f"PRAGMA table_info('{table}')").fetchall()
        varchar_columns = [str(row[1]) for row in columns if str(row[2]).upper() == "VARCHAR"]
        if not varchar_columns:
            output[table] = 0
            continue
        total = " + ".join(
            f'COALESCE(SUM(strlen("{column.replace(chr(34), chr(34) * 2)}")), 0)'
            for column in varchar_columns
        )
        row = connection.execute(f'SELECT {total} FROM "{table}"').fetchone()
        output[table] = 0 if row is None else int(row[0] or 0)
    return output


def _source_v1_logical_bytes(source: LocalStorage) -> dict[str, int]:
    values: dict[str, int] = {}
    for table in ("metric_results", "diagnostic_results"):
        row = source.store.connection.execute(
            f"SELECT COALESCE(SUM(strlen(document_json)), 0) FROM {table}"
        ).fetchone()
        values[table] = 0 if row is None else int(row[0] or 0)
    return values


def _query_archive_smoke(
    archive: HistoricalAnalyticalArchive, staging: RawV2Staging, metric_count: int
) -> dict[str, object]:
    metrics_before = archive.list_metrics_page(
        limit=12, known_to=_BASE_CUT, asset_id="equity:us:aapl"
    )
    metrics_after = archive.list_metrics_page(
        limit=12, known_to=_LATER_CUT, asset_id="equity:us:aapl"
    )
    all_metric_count = archive.count_metrics()
    all_diagnostic_count = archive.count_diagnostics()
    base_count_row = staging._connection.execute(
        "SELECT count(*) FROM historical_metric_results WHERE asset_id = ? AND available_at <= ?",
        ["equity:us:aapl", _BASE_CUT.isoformat()],
    ).fetchone()
    later_count_row = staging._connection.execute(
        "SELECT count(*) FROM historical_metric_results WHERE asset_id = ? AND available_at <= ?",
        ["equity:us:aapl", _LATER_CUT.isoformat()],
    ).fetchone()
    if base_count_row is None or later_count_row is None:
        raise SmokeError("point-in-time count query did not return a row")
    base_count, later_count = int(base_count_row[0]), int(later_count_row[0])
    if len(metrics_before) != min(base_count, 12):
        raise SmokeError("point-in-time base cut returned an unexpected AAPL page")
    if len(metrics_after) != min(later_count, 12):
        raise SmokeError("point-in-time later cut returned an unexpected AAPL page")
    if len(metrics_after) < len(metrics_before):
        raise SmokeError("later point-in-time cut lost previously visible metrics")
    if not {item.result_id for item in metrics_before} <= {
        item.result_id for item in metrics_after
    }:
        raise SmokeError("later point-in-time cut excludes rows visible at the base cut")
    diagnostic_base_count = staging._connection.execute(
        "SELECT count(*) FROM historical_diagnostic_results "
        "WHERE asset_id = ? AND mode = ? AND available_at <= ?",
        ["equity:us:aapl", DiagnosticMode.MARKET.value, _BASE_CUT.isoformat()],
    ).fetchone()
    diagnostic_later_count = staging._connection.execute(
        "SELECT count(*) FROM historical_diagnostic_results "
        "WHERE asset_id = ? AND mode = ? AND available_at <= ?",
        ["equity:us:aapl", DiagnosticMode.MARKET.value, _LATER_CUT.isoformat()],
    ).fetchone()
    if diagnostic_base_count is None or diagnostic_later_count is None:
        raise SmokeError("point-in-time diagnostic counts did not return a row")
    diagnostics_before = archive.list_diagnostics_page(
        limit=12,
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET.value,
        known_to=_BASE_CUT,
    )
    diagnostics_after = archive.list_diagnostics_page(
        limit=12,
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET.value,
        known_to=_LATER_CUT,
    )
    if len(diagnostics_before) != min(int(diagnostic_base_count[0]), 12):
        raise SmokeError("point-in-time base cut returned an unexpected diagnostic page")
    if len(diagnostics_after) != min(int(diagnostic_later_count[0]), 12):
        raise SmokeError("point-in-time later cut returned an unexpected diagnostic page")
    if not {item.diagnostic_id for item in diagnostics_before} <= {
        item.diagnostic_id for item in diagnostics_after
    }:
        raise SmokeError("later point-in-time cut excludes previously visible diagnostics")
    shared = staging._connection.execute(
        "SELECT sequence_id, item_count FROM historical_analytical_sequences "
        "WHERE link_type = 'metric-input-observation-v1' AND item_count = 720"
    ).fetchall()
    if len(shared) != 1:
        raise SmokeError("the shared 720-observation sequence was not interned once")
    sequence_id = str(shared[0][0])
    linked_metrics = staging._connection.execute(
        "SELECT count(*) FROM historical_metric_results WHERE observation_sequence_id = ?",
        [sequence_id],
    ).fetchone()
    segment_count = staging._connection.execute(
        "SELECT count(*) FROM historical_analytical_sequence_segments WHERE sequence_id = ?",
        [sequence_id],
    ).fetchone()
    if linked_metrics is None or int(linked_metrics[0]) != 100:
        raise SmokeError("100 metrics do not reuse the common observation sequence")
    if segment_count is None or int(segment_count[0]) != 3:
        raise SmokeError("the 720-observation sequence does not have exactly 3 segments")
    diagnostics_by_mode = staging._connection.execute(
        "SELECT mode, count(*) FROM historical_diagnostic_results GROUP BY mode ORDER BY mode"
    ).fetchall()
    return {
        "metric_count": all_metric_count,
        "diagnostic_count": all_diagnostic_count,
        "aapl_visible_at_base_cut": base_count,
        "aapl_visible_at_later_cut": later_count,
        "query_page_rows": {
            "metrics_base_cut": len(metrics_before),
            "metrics_later_cut": len(metrics_after),
            "diagnostics_base_cut": len(diagnostics_before),
            "diagnostics_later_cut": len(diagnostics_after),
        },
        "diagnostics_by_mode": {str(mode): int(count) for mode, count in diagnostics_by_mode},
        "shared_720_observation_sequence_count": len(shared),
        "metrics_reusing_shared_sequence": int(linked_metrics[0]) if linked_metrics else 0,
        "shared_sequence_segments": int(segment_count[0]) if segment_count else 0,
    }


def _instrument_importer(importer: HistoricalAnalyticalImporter, tracker: QueryTracker) -> None:
    phase_by_method = {
        "_verify_raw_and_observation_prerequisites": "historical_prerequisites",
        "_scan_source_metrics": "historical_source_metric_scan",
        "_scan_source_diagnostics": "historical_source_diagnostic_scan",
        "_import_metrics": "historical_metric_pages",
        "_import_diagnostics": "historical_diagnostic_pages",
        "_verify_inventory": "historical_verification",
    }
    for method_name, phase_name in phase_by_method.items():
        original = getattr(importer, method_name)

        def measured(*args: object, _original=original, _phase=phase_name, **kwargs: object):
            with tracker.phase(_phase):
                return _original(*args, **kwargs)

        setattr(importer, method_name, measured)


def _new_staging(path: Path, tracker: QueryTracker) -> RawV2Staging:
    path.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect(str(path / "raw-v2-index.duckdb"))
    staging = RawV2Staging(path, _CountedConnection(connection, tracker, "staging"))
    with tracker.phase("staging_open"):
        staging.open()
    _instrument_staging(staging, tracker)
    return staging


def _close_staging(staging: RawV2Staging) -> None:
    staging.close()
    staging._connection.close()


def _restore_staging(
    service: RawV2StagingBackupService,
    backup_path: Path,
    destination: Path,
    tracker: QueryTracker,
) -> tuple[RawV2Staging, object]:
    with tracker.phase("backup_restore"), tracker.count_restore_connections():
        manifest = service.restore(backup_path, destination)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(destination, _CountedConnection(connection, tracker, "staging"))
    with tracker.phase("restored_staging_open"):
        staging.open()
    _instrument_staging(staging, tracker)
    return staging, manifest


def _backup_staging(
    service: RawV2StagingBackupService,
    staging: RawV2Staging,
    backup_path: Path,
    tracker: QueryTracker,
    phase_name: str,
) -> object:
    with tracker.phase(phase_name):
        return service.create(staging, staging._connection, backup_path)


def _run_profile(
    scratch_root: Path,
    profile: str,
    metric_count: int,
    *,
    exercise_resume: bool,
) -> dict[str, object]:
    profile_root = scratch_root / profile
    profile_root.mkdir()
    source_root, workspace_id, source_digest_before, fixture_stats = _seed_source(
        profile_root / "source", profile, metric_count
    )
    source = LocalStorage(StoragePaths.from_root(source_root), read_only=True).open()
    tracker = QueryTracker()
    _instrument_source(source, tracker)
    service = RawV2StagingBackupService()
    staging = _new_staging(profile_root / "seed-staging", tracker)
    source_fingerprint = _tree_digest(source_root.parent)
    try:
        with tracker.phase("raw_import"):
            raw_summary = RawV2Importer(
                source,
                staging,
                source_workspace_id=workspace_id,
                source_fingerprint=source_fingerprint,
            ).run(page_limit=256)
        with tracker.phase("observation_import"):
            observation_summary = ObservationV2Importer(
                source,
                staging,
                source_workspace_id=workspace_id,
                source_fingerprint=source_fingerprint,
                raw_digest=raw_summary.corpus_digest,
            ).run(page_limit=256)
        if not observation_summary.complete:
            raise SmokeError("raw observation staging did not verify complete")
        raw_observation_backup = profile_root / "raw-observation-backup"
        _backup_staging(service, staging, raw_observation_backup, tracker, "raw_observation_backup")
        _close_staging(staging)
        staging, base_manifest = _restore_staging(
            service,
            raw_observation_backup,
            profile_root / "candidate-staging",
            tracker,
        )
        if base_manifest.schema_version == "raw-v2-staging-backup-manifest-v1":
            raise SmokeError("pre-import restore did not preserve raw and observation staging")

        failure_injected = False
        partial_durable_metrics = 0

        def interrupt_once(point: str) -> None:
            nonlocal failure_injected
            if (
                exercise_resume
                and not failure_injected
                and point == "after_metric_page_commit_before_state"
            ):
                failure_injected = True
                raise HistoricalAnalyticalImportError(
                    "smoke interruption after durable page commit"
                )

        importer = HistoricalAnalyticalImporter(
            source,
            staging,
            page_limit=256,
            clock=lambda: _BASE_CUT + timedelta(days=5),
            failure_injector=interrupt_once if exercise_resume else None,
        )
        _instrument_importer(importer, tracker)
        if exercise_resume:
            try:
                with tracker.phase("interrupted_import"):
                    importer.run()
            except HistoricalAnalyticalImportError as error:
                if "smoke interruption" not in str(error):
                    raise
            else:
                raise SmokeError("resume profile did not exercise the durable write boundary")
            partial_durable_metrics = staging.historical_analytical_archive().count_metrics()
            partial_backup = profile_root / "partial-history-backup"
            partial_manifest = _backup_staging(
                service, staging, partial_backup, tracker, "partial_history_backup"
            )
            if partial_manifest.schema_version != "raw-v2-staging-backup-manifest-v6":
                raise SmokeError("partial historical archive did not produce a v6 backup")
            _close_staging(staging)
            staging, restored_partial_manifest = _restore_staging(
                service,
                partial_backup,
                profile_root / "resumed-staging",
                tracker,
            )
            if restored_partial_manifest.backup_id != partial_manifest.backup_id:
                raise SmokeError("partial backup restore changed its stable manifest identity")
            resumed_importer = HistoricalAnalyticalImporter(
                source,
                staging,
                page_limit=256,
                clock=lambda: _BASE_CUT + timedelta(days=6),
            )
            _instrument_importer(resumed_importer, tracker)
            with tracker.phase("historical_resume"):
                summary = resumed_importer.run()
        else:
            with tracker.phase("historical_import"):
                summary = importer.run()

        if summary.metric_count != metric_count or summary.diagnostic_count != metric_count:
            raise SmokeError("historical import count does not match the selected profile")
        if summary.max_page_requested != 256 or summary.max_page_hydrated > 256:
            raise SmokeError("historical importer exceeded its page bound")
        if summary.metrics_by_uuid_version.get("8", 0) != 1:
            raise SmokeError("UUIDv8 public-helper identity was not preserved exactly once")

        archive = staging.historical_analytical_archive()
        with tracker.phase("point_in_time_and_shared_sequence_queries"):
            query_evidence = _query_archive_smoke(archive, staging, metric_count)
        with tracker.phase("complete_archive_verification"):
            complete_summary = archive.verify_complete()
        if not complete_summary.complete:
            raise SmokeError("complete archive verification failed")

        full_backup = profile_root / "complete-history-backup"
        full_manifest = _backup_staging(service, staging, full_backup, tracker, "complete_backup")
        if full_manifest.schema_version != "raw-v2-staging-backup-manifest-v6":
            raise SmokeError("complete historical archive did not produce a v6 backup")
        _close_staging(staging)
        staging, restored_manifest = _restore_staging(
            service,
            full_backup,
            profile_root / "verified-staging",
            tracker,
        )
        if restored_manifest.backup_id != full_manifest.backup_id:
            raise SmokeError("complete backup restore changed its stable manifest identity")
        with tracker.phase("restored_archive_verification"):
            restored_archive = staging.historical_analytical_archive()
            restored_summary = restored_archive.verify_complete()
            logical_archive_bytes = _logical_varchar_bytes(staging._connection)
            v1_bytes = _source_v1_logical_bytes(source)
            actual_sequence_count = int(
                staging._connection.execute(
                    "SELECT count(*) FROM historical_analytical_sequences"
                ).fetchone()[0]
            )
            actual_segment_count = int(
                staging._connection.execute(
                    "SELECT count(*) FROM historical_analytical_segments"
                ).fetchone()[0]
            )
        if restored_summary.metric_count != metric_count:
            raise SmokeError("restored archive does not contain the full metric inventory")
        if actual_sequence_count != fixture_stats["unique_sequences"]:
            raise SmokeError("content-addressed sequence reuse differs from fixture inventory")
        if actual_segment_count != fixture_stats["unique_segments"]:
            raise SmokeError("content-addressed segment reuse differs from fixture inventory")

        segment_member_selects = tracker.segment_member_selects
        query_bound = tracker.max_query_parameters
        pages_per_family = (metric_count + 255) // 256
        member_query_limit = 72 * pages_per_family + 32
        if segment_member_selects > member_query_limit:
            raise SmokeError(
                f"segment member reads exceed chunk bound: "
                f"{segment_member_selects} > {member_query_limit}; "
                f"phases={tracker.segment_member_selects_by_phase}"
            )
        single_member_selects = sum(
            batch_size == 1 for batch_size in tracker.segment_member_batch_sizes
        )
        single_member_query_limit = 12 * pages_per_family + 12
        if single_member_selects > single_member_query_limit:
            raise SmokeError(
                f"single-segment member reads exceed chunk bound: "
                f"{single_member_selects} > {single_member_query_limit}"
            )
        if query_bound > 256 or tracker.max_batch_rows > 256:
            raise SmokeError("observed SQL query or insert batch exceeded 256")
        if tracker.unbounded_document_reads:
            raise SmokeError("smoke observed an unbounded v1 document_json scan")
        source_digest_after = _tree_digest(source_root.parent)
        if source_digest_after != source_digest_before:
            raise SmokeError("historical import modified the read-only source workspace")

        return {
            "profile": profile,
            "metric_count": summary.metric_count,
            "diagnostic_count": summary.diagnostic_count,
            "page_size_limit": 256,
            "max_page_hydrated": summary.max_page_hydrated,
            "metrics_by_key": summary.metrics_by_key,
            "metrics_by_asset": summary.metrics_by_asset,
            "metrics_by_uuid_version": summary.metrics_by_uuid_version,
            "diagnostics_by_asset": summary.diagnostics_by_asset,
            "diagnostics_by_mode": summary.diagnostics_by_mode,
            "created_reused": {
                "metrics_created_across_attempts": (
                    partial_durable_metrics + summary.metric_created_count
                ),
                "metrics_reused": summary.metric_reused_count,
                "diagnostics_created": summary.diagnostic_created_count,
                "diagnostics_reused": summary.diagnostic_reused_count,
            },
            "query_evidence": query_evidence,
            "tracker": tracker.snapshot(),
            "shared_link_sequence": {
                "length": 720,
                "shared_by_metrics": 100,
                "segments": 3,
            },
            "archive_logical_varchar_bytes_by_table": logical_archive_bytes,
            "v1_logical_document_json_bytes": v1_bytes,
            "source": {
                "workspace_id": workspace_id,
                "fingerprint": summary.source_fingerprint,
                "raw_digest": summary.raw_digest,
                "observation_digest": summary.observation_digest,
                "metric_digest": summary.metric_digest,
                "diagnostic_digest": summary.diagnostic_digest,
                "tree_digest_before": source_digest_before,
                "tree_digest_after": source_digest_after,
            },
            "fixture_lineage_counts": fixture_stats,
            "archive_lineage_counts": {
                "sequences_stored": actual_sequence_count,
                "segments_stored": actual_segment_count,
                "sequences_reused_from_duplicate_requests": fixture_stats["reused_sequences"],
                "segments_reused_from_duplicate_requests": fixture_stats["reused_segments"],
            },
            "physical_bytes": {
                "restored_staging_database": sum(
                    path.stat().st_size
                    for path in staging.destination.glob("*.duckdb")
                    if path.is_file()
                ),
                "restored_staging_wal": sum(
                    path.stat().st_size
                    for path in staging.destination.glob("*.wal")
                    if path.is_file()
                ),
                "complete_backup": _directory_bytes(full_backup),
                "temporary_files": sum(
                    path.stat().st_size
                    for path in profile_root.rglob("*")
                    if path.is_file() and (".tmp" in path.name or path.suffix == ".tmp")
                ),
            },
            "backup_schema": full_manifest.schema_version,
            "partial_backup_exercised": exercise_resume,
            "partial_durable_metrics_recovered": partial_durable_metrics,
            "backup_identity": {
                "backup_id": str(full_manifest.backup_id),
                "staging_id": full_manifest.staging_id,
                "historical_state_digest": (
                    full_manifest.historical_analytical_counts.state_digest
                    if full_manifest.historical_analytical_counts is not None
                    else None
                ),
            },
            "source_workspace_unchanged": source_digest_after == source_digest_before,
            "scratch_bytes": _directory_bytes(profile_root),
        }
    finally:
        if staging.is_open:
            staging.close()
        with suppress(AttributeError, duckdb.Error):
            staging._connection.close()
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    output = arguments.output.expanduser().resolve(strict=False)
    try:
        output.relative_to(repository)
    except ValueError:
        pass
    else:
        raise SmokeError("smoke output must be outside the repository")
    if output.exists():
        raise SmokeError("smoke output already exists; choose a new artifact path")
    output.parent.mkdir(parents=True, exist_ok=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise SmokeError("run the final smoke from the clean candidate worktree")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    actual_command = " ".join(shlex.quote(argument) for argument in (sys.executable, *sys.argv))
    with tempfile.TemporaryDirectory(prefix="data-chassis-39-smoke-") as temporary:
        scratch = Path(temporary)
        profiles = [
            _run_profile(scratch, "historical_analytical_fidelity", 257, exercise_resume=False),
            _run_profile(scratch, "historical_analytical_resume", 257, exercise_resume=True),
            _run_profile(scratch, "historical_analytical_scale", 1537, exercise_resume=False),
        ]
        report = {
            "schema_version": "historical-analytical-import-smoke-v1",
            "command": actual_command,
            "head": head,
            "environment": {
                "python": platform.python_version(),
                "duckdb": duckdb.__version__,
                "platform": platform.platform(),
            },
            "profiles": profiles,
            "host_timing_and_rss_are_gates": False,
        }
    temporary_output = output.with_name(f".{output.name}.tmp")
    temporary_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary_output, output)
    print(json.dumps({"artifact": str(output), "head": head, "profiles": len(profiles)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
