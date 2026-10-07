"""Run the isolated incremental-runtime-v2 acceptance probes and save their evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID

import duckdb
from pydantic import BaseModel

from investment_analyst.core.models import MetricResult

_FAMILY_TESTS = {
    "market_daily": (
        "tests/integration/analytics/test_incremental_runtime_v2_integration.py",
        "tests/integration/analytics/market/test_market_identity_v2_adoption.py",
        "tests/integration/analytics/test_existing_metric_lookup_by_identity.py",
    ),
    "derivatives": (
        "tests/integration/analytics/crypto/test_derivatives_pipeline.py",
        "tests/integration/analytics/crypto/test_derivatives_identity_v2_adoption.py",
    ),
    "fundamentals": (
        "tests/integration/providers/test_sec_fundamental_metric_pipeline_integration.py",
    ),
    "valuation": (
        "tests/unit/analytics/valuation/test_persistence.py",
        "tests/unit/analytics/valuation/test_semantic_identity_v2.py",
    ),
    "institutions_and_weights": (
        "tests/integration/analytics/test_institutional_metric_pipeline_integration.py",
        "tests/integration/analytics/test_institutional_weight_pipeline_integration.py",
        "tests/unit/analytics/cazatiburones/test_institutional_metric_identity.py",
        "tests/unit/analytics/cazatiburones/test_institutional_weight_identity.py",
    ),
    "activity_and_events": (
        "tests/integration/analytics/test_activity_metric_pipeline_integration.py",
        "tests/integration/analytics/test_activity_event_flow.py",
        "tests/integration/analytics/test_institutional_event_service_integration.py",
        "tests/unit/analytics/cazatiburones/test_activity_metric_identity.py",
    ),
    "access_and_recovery": (
        "tests/unit/analytics/test_analytical_access_unit.py",
        "tests/unit/storage/test_workspace_incremental_v2_unit.py",
        "tests/unit/workspace/test_workspace_v2_backup_unit.py",
    ),
}
_EXPECTED_FAMILY_PIPELINES = {
    "market_daily": {"MarketStatisticsPipeline.run"},
    "derivatives": {"CryptoDerivativesMetricPipeline.run"},
    "fundamentals": {"SecIssuerFundamentalMetricPipeline.run"},
    "valuation": {"CorporateValuationPersistencePipeline.persist"},
    "institutions_and_weights": {
        "InstitutionalMetricPipeline.compute",
        "InstitutionalWeightPipeline.compute",
    },
    "activity_and_events": {
        "ActivityMetricPipeline.compute",
        "ActivityEventService.materialize",
        "InstitutionalEventService.materialize",
    },
}
_REQUIRED_POSITIVE_FAMILY_PIPELINES = {
    "market_daily": {"MarketStatisticsPipeline.run"},
    "derivatives": {"CryptoDerivativesMetricPipeline.run"},
    "fundamentals": {"SecIssuerFundamentalMetricPipeline.run"},
    "valuation": {"CorporateValuationPersistencePipeline.persist"},
    "institutions_and_weights": {
        "InstitutionalMetricPipeline.compute",
        "InstitutionalWeightPipeline.compute",
    },
    "activity_and_events": {
        "ActivityMetricPipeline.compute",
        "ActivityEventService.materialize",
        "InstitutionalEventService.materialize",
    },
}


class SmokeError(RuntimeError):
    """The requested smoke run cannot be completed safely or did not pass."""


_FAMILY_EVENTS: list[dict[str, object]] = []
_FAMILY_TEST_STATUS: dict[str, str] = {}
_CURRENT_TEST_NODEID: str | None = None
_ACTIVE_FAMILY_EVENT: dict[str, object] | None = None
_PATCHED_METRIC_REPOSITORY_TYPES: set[type[object]] = set()
_PATCHED_METRIC_REPOSITORY_INSTANCE_METHODS: set[tuple[int, str]] = set()


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, UUID, Decimal)):
        return str(value) if isinstance(value, Decimal | UUID) else value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _canonical_digest(value: object) -> str:
    canonical = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_id(value: object) -> str | None:
    for name in (
        "result_id",
        "event_id",
        "candidate_id",
        "snapshot_id",
        "observation_id",
        "record_id",
        "raw_record_id",
        "artifact_id",
        "reference_id",
    ):
        identifier = getattr(value, name, None)
        if identifier is not None:
            return str(identifier)
    return None


def _input_identity(value: object) -> object:
    observation = getattr(value, "observation", None)
    row = getattr(value, "row", None)
    artifact = getattr(value, "artifact", None)
    if observation is not None and row is not None:
        return {
            "kind": "institutional-observation-view",
            "observation": _input_identity(observation),
            "artifact": _input_identity(artifact) if artifact is not None else None,
            "row": {
                name: _json_value(getattr(row, name, None))
                for name in (
                    "row_number",
                    "cusip",
                    "title_of_class",
                    "put_call",
                    "investment_discretion",
                    "other_manager_sequence_references",
                )
            },
        }
    if hasattr(value, "observation_id") and hasattr(value, "field_name"):
        source = getattr(value, "source", None)
        return {
            "kind": "normalized-observation",
            "asset_id": _json_value(getattr(value, "asset_id", None)),
            "source_id": _json_value(getattr(source, "source_id", None)),
            "field_name": _json_value(getattr(value, "field_name", None)),
            "frequency": _json_value(getattr(value, "frequency", None)),
            "observed_at": _json_value(getattr(value, "observed_at", None)),
            "period_start": _json_value(getattr(value, "period_start", None)),
            "period_end": _json_value(getattr(value, "period_end", None)),
            "transformation_version": _json_value(getattr(value, "transformation_version", None)),
        }
    if hasattr(value, "manager_cik") and hasattr(value, "report_period"):
        return {
            "kind": "institutional-report",
            "manager_cik": _json_value(getattr(value, "manager_cik", None)),
            "form": _json_value(getattr(value, "form", None)),
            "report_period": _json_value(getattr(value, "report_period", None)),
        }
    if hasattr(value, "record_id") and hasattr(value, "schema_version"):
        source = getattr(value, "source", None)
        payload = getattr(value, "payload", None)
        payload_coordinates = (
            {
                key: _json_value(payload[key])
                for key in (
                    "event_time",
                    "eventTime",
                    "event_type",
                    "form",
                    "period",
                    "periodEnd",
                    "reportCalendarOrQuarter",
                    "reportDate",
                    "symbol",
                )
                if key in payload
            }
            if isinstance(payload, Mapping)
            else {}
        )
        identity = {
            "kind": "raw-record",
            "asset_id": _json_value(getattr(value, "asset_id", None)),
            "source_id": _json_value(getattr(source, "source_id", None)),
            "schema_version": _json_value(getattr(value, "schema_version", None)),
            "event_time": _json_value(getattr(value, "event_time", None)),
            "payload_coordinates": payload_coordinates,
        }
        if identity["event_time"] is None and not payload_coordinates:
            identity["record_id"] = _result_id(value)
        return identity
    if isinstance(value, MetricResult):
        return {"kind": "metric-result", "result_id": str(value.result_id)}
    identifier = _result_id(value)
    if identifier is not None:
        return {"kind": type(value).__qualname__, "identifier": identifier}
    return {"kind": type(value).__qualname__, "content": _canonical_digest(value)}


def _input_models(value: object) -> tuple[object, ...]:
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(value)
    if isinstance(value, BaseModel):
        return (value,)
    return ()


def _record_input_models(event: dict[str, object], value: object) -> tuple[object, ...]:
    models = _input_models(value)
    input_records = event["input_records"]
    if isinstance(input_records, dict):
        for model in models:
            identity = _canonical_digest(_input_identity(model))
            input_records[identity] = _canonical_digest(model)
    identifiers = event["input_identifiers"]
    if isinstance(identifiers, set):
        identifiers.update(
            identifier for model in models if (identifier := _result_id(model)) is not None
        )
    return models


def _metric_models(values: object) -> tuple[MetricResult, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return ()
    return tuple(item for item in values if isinstance(item, MetricResult))


def _record_metric_repository_call(
    method_name: str,
    original: Callable[..., object],
    repository: object,
    *args: object,
    **kwargs: object,
) -> object:
    result = original(repository, *args, **kwargs)
    event = _ACTIVE_FAMILY_EVENT
    if event is None:
        return result
    query_counts = event["repository_query_counts"]
    if isinstance(query_counts, Counter):
        query_counts[method_name] += 1
    if method_name in {"get_existing", "list_ids"}:
        event["candidate_lookup_page_count"] = int(event["candidate_lookup_page_count"]) + 1
    elif method_name in {"get_many", "list"}:
        event["metric_lookup_page_count"] = int(event["metric_lookup_page_count"]) + 1
    candidate_ids = event["candidate_ids"]
    existing_ids = event["existing_ids"]
    created_ids = event["created_ids"]
    result_digests = event["result_digests"]
    batch_sizes = event["batch_sizes"]
    if method_name == "get_existing" and args:
        requested = args[0]
        if isinstance(requested, Collection) and not isinstance(requested, str | bytes):
            if isinstance(candidate_ids, set):
                candidate_ids.update(str(item) for item in requested)
            if isinstance(batch_sizes, list):
                batch_sizes.append(len(requested))
        if isinstance(result, Mapping) and isinstance(existing_ids, set):
            existing_ids.update(str(item) for item in result)
            if isinstance(result_digests, list):
                result_digests.extend(
                    (str(identifier), _canonical_digest(value.model_dump(mode="json")))
                    for identifier, value in result.items()
                    if isinstance(value, MetricResult)
                )
    elif method_name in {"save", "save_many"} and args:
        values = (args[0],) if method_name == "save" else args[0]
        models = _metric_models(values)
        if isinstance(batch_sizes, list):
            batch_sizes.append(len(models))
        if isinstance(created_ids, set):
            for model in models:
                created_ids.add(str(model.result_id))
                if isinstance(result_digests, list):
                    result_digests.append(
                        (str(model.result_id), _canonical_digest(model.model_dump(mode="json")))
                    )
    elif method_name in {"get_many", "list", "list_ids"}:
        hydrated = len(result) if isinstance(result, (Mapping, Sequence)) else 0
        event["metric_models_hydrated"] = int(event["metric_models_hydrated"]) + (
            0 if method_name == "list_ids" else hydrated
        )
        if isinstance(batch_sizes, list):
            if method_name == "get_many" and args:
                batch_sizes.append(len(args[0]))
            elif method_name == "list":
                batch_sizes.append(hydrated)
        if method_name in {"get_many", "list"}:
            _record_input_models(event, result)
    return result


def _instrument_class_method(
    target: type[object],
    method_name: str,
    wrapper_factory: Callable[[Callable[..., object]], Callable[..., object]],
) -> None:
    original = getattr(target, method_name, None)
    if callable(original):
        setattr(target, method_name, wrapper_factory(original))


def _instrument_dynamic_metric_repository(target: type[object]) -> None:
    if target in _PATCHED_METRIC_REPOSITORY_TYPES:
        return
    for method_name in ("get_existing", "get_many", "list", "list_ids", "save", "save_many"):
        _instrument_class_method(
            target,
            method_name,
            lambda original, method_name=method_name: (
                lambda instance, *args, **kwargs: _record_metric_repository_call(
                    method_name, original, instance, *args, **kwargs
                )
            ),
        )
    _PATCHED_METRIC_REPOSITORY_TYPES.add(target)


def _instrument_dynamic_metric_repository_instance(instance: object) -> None:
    instance_fields = getattr(instance, "__dict__", {})
    if not isinstance(instance_fields, dict):
        return
    for method_name in ("get_existing", "get_many", "list", "list_ids", "save", "save_many"):
        key = (id(instance), method_name)
        original = getattr(instance, method_name, None)
        if method_name not in instance_fields or key in _PATCHED_METRIC_REPOSITORY_INSTANCE_METHODS:
            continue
        if not callable(original):
            continue

        def wrapped(
            *args: object,
            _original: Callable[..., object] = original,
            _method_name: str = method_name,
            _instance: object = instance,
            **kwargs: object,
        ) -> object:
            def call_original(
                repository: object, *call_args: object, **call_kwargs: object
            ) -> object:
                del repository
                return _original(*call_args, **call_kwargs)

            return _record_metric_repository_call(
                _method_name,
                call_original,
                _instance,
                *args,
                **kwargs,
            )

        setattr(instance, method_name, wrapped)
        _PATCHED_METRIC_REPOSITORY_INSTANCE_METHODS.add(key)


def _instrument_input_method(target: type[object], method_name: str, input_name: str) -> None:
    original = getattr(target, method_name, None)
    if not callable(original):
        return

    def wrapped(instance: object, *args: object, **kwargs: object) -> object:
        result = original(instance, *args, **kwargs)
        event = _ACTIVE_FAMILY_EVENT
        if event is not None:
            counts = event["input_read_counts"]
            if isinstance(counts, Counter):
                counts[input_name] += 1
            models = _record_input_models(event, result)
            rows = len(models)
            event["input_rows_loaded"] = int(event["input_rows_loaded"]) + rows
            event["input_page_count"] = int(event["input_page_count"]) + 1
            event["input_page_size_max"] = max(int(event["input_page_size_max"]), rows)
        return result

    setattr(target, method_name, wrapped)


def _instrument_event_repository(target: type[object]) -> None:
    original = getattr(target, "save", None)
    if not callable(original):
        return

    def wrapped(instance: object, snapshot: object, *args: object, **kwargs: object) -> object:
        result = original(instance, snapshot, *args, **kwargs)
        event = _ACTIVE_FAMILY_EVENT
        if event is not None:
            counts = event["repository_query_counts"]
            if isinstance(counts, Counter):
                counts["event_snapshot_save"] += 1
            events = getattr(snapshot, "events", ())
            candidates = getattr(snapshot, "candidates", ())
            event_ids = sorted(
                identifier for item in events if (identifier := _result_id(item)) is not None
            )
            candidate_ids = sorted(
                identifier for item in candidates if (identifier := _result_id(item)) is not None
            )
            event["event_snapshot_id"] = _result_id(snapshot)
            event["event_count"] = len(events)
            event["event_ids_sha256"] = _canonical_digest(event_ids)
            event["event_id_samples"] = event_ids[:3]
            event["event_candidate_count"] = len(candidates)
            event["event_candidate_ids_sha256"] = _canonical_digest(candidate_ids)
            event["event_snapshot_sha256"] = _canonical_digest(snapshot)
        return result

    target.save = wrapped


def _instrument_pipeline(target: type[object], method_name: str, family: str) -> None:
    def factory(original: Callable[..., object]) -> Callable[..., object]:
        def wrapped(instance: object, *args: object, **kwargs: object) -> object:
            global _ACTIVE_FAMILY_EVENT
            event: dict[str, object] = {
                "family": family,
                "pipeline": f"{target.__name__}.{method_name}",
                "test": _CURRENT_TEST_NODEID,
                "started_at": datetime.now(UTC).isoformat(),
                "repository_query_counts": Counter(),
                "input_read_counts": Counter(),
                "input_rows_loaded": 0,
                "input_page_count": 0,
                "input_page_size_max": 0,
                "candidate_lookup_page_count": 0,
                "metric_lookup_page_count": 0,
                "metric_models_hydrated": 0,
                "candidate_ids": set(),
                "existing_ids": set(),
                "created_ids": set(),
                "input_identifiers": set(),
                "result_digests": [],
                "batch_sizes": [],
                "input_records": {},
            }
            previous_event = _ACTIVE_FAMILY_EVENT
            _ACTIVE_FAMILY_EVENT = event
            started = perf_counter_ns()
            storage = getattr(instance, "_storage", getattr(instance, "storage", None))
            metric_repository = getattr(storage, "metric_results", None)
            if metric_repository is not None:
                _instrument_dynamic_metric_repository(type(metric_repository))
                _instrument_dynamic_metric_repository_instance(metric_repository)
            input_metrics: list[tuple[str, str]] = []
            for argument in args:
                metrics = getattr(argument, "metrics", ())
                if isinstance(metrics, Sequence) and not isinstance(metrics, str | bytes):
                    for metric in metrics:
                        identifier = _result_id(metric)
                        if identifier is not None:
                            input_metrics.append((identifier, _canonical_digest(metric)))
            if input_metrics:
                event["candidate_ids"].update(identifier for identifier, _ in input_metrics)
                event["input_metric_digests"] = input_metrics
                input_records = event["input_records"]
                if isinstance(input_records, dict):
                    input_records.update(
                        {
                            _canonical_digest({"metric_result_id": identifier}): digest
                            for identifier, digest in input_metrics
                        }
                    )
                input_identifiers = event["input_identifiers"]
                if isinstance(input_identifiers, set):
                    input_identifiers.update(identifier for identifier, _ in input_metrics)
            try:
                result = original(instance, *args, **kwargs)
            except BaseException as error:
                event["error_type"] = type(error).__name__
                raise
            else:
                summary: dict[str, object] = {}
                for name in (
                    "asset_id",
                    "manager_cik",
                    "known_at",
                    "as_of_from",
                    "as_of_before",
                    "metrics_generated",
                    "results_generated",
                    "metrics_created",
                    "results_created",
                    "metric_results_created",
                    "metrics_reused",
                    "results_reused",
                    "metric_results_reused",
                    "values_examined",
                    "events",
                    "candidates",
                    "missing_requirements",
                    "skipped_total",
                    "result_count",
                    "result_ids_sha256",
                    "result_content_sha256",
                    "result_id_samples",
                ):
                    value = getattr(result, name, None)
                    if value is not None:
                        summary[name] = _json_value(value)
                for name in (
                    "asset_id",
                    "manager_cik",
                    "known_at",
                    "as_of_from",
                    "as_of_before",
                    "source_id",
                    "frequency",
                    "form",
                    "forms",
                    "start",
                    "end",
                    "limit",
                    "page_size",
                    "metric_keys",
                    "funding_source_id",
                    "dvol_source_id",
                    "summary_source_id",
                ):
                    value = kwargs.get(name)
                    if value is None:
                        for argument in args:
                            candidates = [
                                argument,
                                getattr(argument, "request", None),
                                getattr(argument, "query", None),
                            ]
                            request = getattr(argument, "request", None)
                            if request is not None:
                                candidates.append(getattr(request, "query", None))
                            value = next(
                                (
                                    candidate_value
                                    for candidate in candidates
                                    if candidate is not None
                                    and (candidate_value := getattr(candidate, name, None))
                                    is not None
                                ),
                                None,
                            )
                            if value is not None:
                                break
                    if value is not None:
                        summary[name] = _json_value(value)
                results = _metric_models(getattr(result, "results", ()))
                if results:
                    event["candidate_ids"].update(str(item.result_id) for item in results)
                    summary["result_count"] = len(results)
                    summary["result_ids_sha256"] = _canonical_digest(
                        sorted(str(item.result_id) for item in results)
                    )
                    summary["result_content_sha256"] = _canonical_digest(
                        [item.model_dump(mode="json") for item in results]
                    )
                    summary["result_id_samples"] = [str(item.result_id) for item in results[:3]]
                for name in ("snapshot_id", "created", "traceability_verified"):
                    value = getattr(result, name, None)
                    if value is not None:
                        summary[name] = _json_value(value)
                result_dump = _json_value(result)
                event["summary"] = summary
                event["summary_sha256"] = _canonical_digest(result_dump)
                return result
            finally:
                event["elapsed_microseconds"] = (perf_counter_ns() - started) // 1_000
                _ACTIVE_FAMILY_EVENT = previous_event
                _FAMILY_EVENTS.append(event)

        return wrapped

    _instrument_class_method(target, method_name, factory)


def _install_family_instrumentation() -> None:
    from investment_analyst.analytics.cazatiburones.activity_event_repository import (
        ActivityEventRepository,
    )
    from investment_analyst.analytics.cazatiburones.activity_event_service import (
        ActivityEventService,
    )
    from investment_analyst.analytics.cazatiburones.activity_metric_pipeline import (
        ActivityMetricPipeline,
    )
    from investment_analyst.analytics.cazatiburones.institutional_event_repository import (
        InstitutionalEventRepository,
    )
    from investment_analyst.analytics.cazatiburones.institutional_event_service import (
        InstitutionalEventService,
    )
    from investment_analyst.analytics.cazatiburones.institutional_metric_pipeline import (
        InstitutionalMetricPipeline,
    )
    from investment_analyst.analytics.cazatiburones.institutional_weight_pipeline import (
        InstitutionalWeightPipeline,
    )
    from investment_analyst.analytics.crypto.derivatives_pipeline import (
        CryptoDerivativesMetricPipeline,
    )
    from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
    from investment_analyst.analytics.valuation.pipeline import (
        CorporateValuationPersistencePipeline,
    )
    from investment_analyst.evidence.sec_institutional_observations.service import (
        InstitutionalObservationService,
    )
    from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
        InstitutionalSemanticsArtifactReader,
    )
    from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
        SecIssuerFundamentalMetricPipeline,
    )
    from investment_analyst.storage.raw_records import JsonRawRecordRepository
    from investment_analyst.storage.repositories import (
        DuckDBMetricResultRepository,
        DuckDBObservationRepository,
    )
    from investment_analyst.storage.workspace_v2_repositories import (
        WorkspaceV2MetricResultRepository,
        WorkspaceV2ObservationRepository,
        WorkspaceV2RawRecordRepository,
    )

    pipeline_methods = (
        (CryptoDerivativesMetricPipeline, "run", "derivatives"),
        (SecIssuerFundamentalMetricPipeline, "run", "fundamentals"),
        (CorporateValuationPersistencePipeline, "persist", "valuation"),
        (InstitutionalMetricPipeline, "compute", "institutions"),
        (InstitutionalWeightPipeline, "compute", "institutional_weights"),
        (ActivityMetricPipeline, "compute", "activity_metrics"),
        (ActivityEventService, "materialize", "activity_events"),
        (InstitutionalEventService, "materialize", "institutional_events"),
        (MarketStatisticsPipeline, "run", "market_daily"),
    )
    for target, method_name, family in pipeline_methods:
        _instrument_pipeline(target, method_name, family)

    for repository in (ActivityEventRepository, InstitutionalEventRepository):
        _instrument_event_repository(repository)

    metric_repositories = (DuckDBMetricResultRepository, WorkspaceV2MetricResultRepository)
    for repository in metric_repositories:
        _instrument_dynamic_metric_repository(repository)

    input_repositories = (
        (DuckDBObservationRepository, "observations"),
        (WorkspaceV2ObservationRepository, "observations"),
        (JsonRawRecordRepository, "raw_records"),
        (WorkspaceV2RawRecordRepository, "raw_records"),
    )
    for repository, input_name in input_repositories:
        for method_name in ("list", "get_many"):
            _instrument_input_method(repository, method_name, input_name)

    institutional_input_methods = (
        (InstitutionalSemanticsArtifactReader, "list_for_manager", "institutional_artifacts"),
        (InstitutionalObservationService, "list_for_manager", "institutional_observation_refs"),
        (
            InstitutionalObservationService,
            "observation_ids_for_references",
            "institutional_observations",
        ),
    )
    for target, method_name, input_name in institutional_input_methods:
        _instrument_input_method(target, method_name, input_name)


def pytest_sessionstart(session: object) -> None:
    del session
    if os.environ.get("INVESTMENT_ANALYST_INCREMENTAL_SMOKE_FAMILY_REPORT"):
        _install_family_instrumentation()


def pytest_runtest_setup(item: object) -> None:
    global _CURRENT_TEST_NODEID
    _CURRENT_TEST_NODEID = str(getattr(item, "nodeid", "unknown"))


def pytest_runtest_makereport(item: object, call: object) -> None:
    if getattr(call, "when", None) == "call":
        nodeid = str(getattr(item, "nodeid", "unknown"))
        _FAMILY_TEST_STATUS[nodeid] = "failed" if getattr(call, "excinfo", None) else "passed"


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    del session
    output_value = os.environ.get("INVESTMENT_ANALYST_INCREMENTAL_SMOKE_FAMILY_REPORT")
    if not output_value:
        return
    finalized: list[dict[str, object]] = []
    previous_input_records: dict[tuple[str, str, str], dict[str, str]] = {}
    for event in _FAMILY_EVENTS:
        nodeid = str(event.get("test", ""))
        comparison_key = (
            str(event.get("family", "")),
            str(event.get("pipeline", "")),
            nodeid,
        )
        raw_input_records = event.pop("input_records", {})
        current_input_records = (
            {str(key): str(value) for key, value in raw_input_records.items()}
            if isinstance(raw_input_records, Mapping)
            else {}
        )
        previous_records = previous_input_records.get(comparison_key)
        event["input_record_count"] = len(current_input_records)
        event["input_record_keys_sha256"] = _canonical_digest(sorted(current_input_records))
        event["input_content_sha256"] = _canonical_digest(sorted(current_input_records.items()))
        raw_input_identifiers = event.pop("input_identifiers", set())
        input_identifiers = (
            sorted(str(identifier) for identifier in raw_input_identifiers)
            if isinstance(raw_input_identifiers, Collection)
            else []
        )
        event["input_ids_sha256"] = _canonical_digest(input_identifiers)
        event["input_id_samples"] = input_identifiers[:3]
        if previous_records is None:
            event["input_comparison"] = "baseline_no_prior_pipeline_call"
            event["new_inputs_count"] = None
            event["input_revision_count"] = None
            event["input_change_scenario"] = "baseline"
        else:
            current_keys = set(current_input_records)
            previous_keys = set(previous_records)
            new_count = len(current_keys - previous_keys)
            revision_count = sum(
                previous_records[key] != current_input_records[key]
                for key in current_keys.intersection(previous_keys)
            )
            event["input_comparison"] = "measured_against_prior_pipeline_call"
            event["new_inputs_count"] = new_count
            event["input_revision_count"] = revision_count
            event["input_change_scenario"] = (
                "new_inputs_and_revisions"
                if new_count and revision_count
                else "new_inputs"
                if new_count
                else "input_revisions"
                if revision_count
                else "no_input_change"
            )
        previous_input_records[comparison_key] = current_input_records
        candidate_ids = sorted(event.pop("candidate_ids"))
        existing_ids = sorted(event.pop("existing_ids"))
        created_ids = sorted(event.pop("created_ids"))
        digests = sorted(event.pop("result_digests"))
        batch_sizes = event.pop("batch_sizes")
        queries = event.pop("repository_query_counts")
        input_queries = event.pop("input_read_counts")
        event["test_status"] = _FAMILY_TEST_STATUS.get(str(event.get("test")), "unknown")
        summary = event.get("summary")
        summary_counts = summary if isinstance(summary, dict) else {}
        generated_count = next(
            (
                int(summary_counts[name])
                for name in ("metrics_generated", "results_generated", "result_count")
                if isinstance(summary_counts.get(name), int)
            ),
            len(candidate_ids),
        )
        created_count = next(
            (
                int(summary_counts[name])
                for name in ("metrics_created", "results_created", "metric_results_created")
                if isinstance(summary_counts.get(name), int)
            ),
            len(created_ids),
        )
        reused_count = next(
            (
                int(summary_counts[name])
                for name in ("metrics_reused", "results_reused", "metric_results_reused")
                if isinstance(summary_counts.get(name), int)
            ),
            len(existing_ids),
        )
        event["calculated_candidate_count"] = generated_count
        event["candidate_count"] = max(len(candidate_ids), generated_count)
        summary_id_digest = summary_counts.get("result_ids_sha256")
        event["candidate_ids_sha256"] = (
            _canonical_digest(candidate_ids)
            if candidate_ids
            else summary_id_digest
            if isinstance(summary_id_digest, str)
            else _canonical_digest(candidate_ids)
        )
        event["candidate_id_samples"] = candidate_ids[:3]
        event["reused_count"] = reused_count
        event["reused_ids_sha256"] = _canonical_digest(existing_ids)
        event["reused_id_samples"] = existing_ids[:3]
        event["created_count"] = created_count
        event["created_ids_sha256"] = _canonical_digest(created_ids)
        event["created_id_samples"] = created_ids[:3]
        event["content_sha256"] = _canonical_digest(digests)
        event["batch_sizes"] = batch_sizes
        event["max_batch_size"] = max(batch_sizes, default=0)
        query_counts = dict(queries)
        input_counts = dict(input_queries)
        event["repository_query_count"] = sum(query_counts.values())
        event["input_query_count"] = sum(input_counts.values())
        event["repository_query_counts"] = dict(queries)
        event["input_read_counts"] = dict(input_queries)
        finalized.append(event)
    report = {
        "schema_version": "incremental-runtime-v2-family-evidence-v1",
        "family": os.environ.get("INVESTMENT_ANALYST_INCREMENTAL_SMOKE_FAMILY", "unknown"),
        "pytest_return_code": exitstatus,
        "tests": _FAMILY_TEST_STATUS,
        "product_runs": finalized,
    }
    Path(output_value).write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_value(root: Path, expression: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", expression],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _output_path(value: str, repository_root: Path) -> Path:
    requested = Path(value).expanduser().absolute()
    for ancestor in (requested, *requested.parents):
        if ancestor.exists() and ancestor.is_symlink():
            raise SmokeError("output path cannot contain a symbolic link")
    target = requested.resolve(strict=False)
    if target.exists() or target.is_symlink():
        raise SmokeError("output path already exists or is a symbolic link")
    if target == repository_root or repository_root in target.parents:
        raise SmokeError("output file must be outside the repository")
    permanent_workspace = Path.home() / ".local/share/investment-analyst/workspaces/default"
    if target == permanent_workspace or permanent_workspace in target.parents:
        raise SmokeError("output file must be outside the permanent workspace")
    if not target.parent.is_dir():
        raise SmokeError("output parent directory must already exist")
    return target


def _rss_high_water_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage if platform.system() == "Darwin" else usage * 1024)


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    started_at: datetime,
) -> dict[str, object]:
    started = perf_counter_ns()
    print(f"SMOKE START {command[1] if len(command) > 1 else command[0]}", flush=True)
    time_binary = Path("/usr/bin/time")
    if not time_binary.is_file():
        raise SmokeError("GNU time is required to measure child-process RSS")
    rss_path = Path(env["TMPDIR"]) / f"rss-{perf_counter_ns()}.txt"
    timed_command = [str(time_binary), "-f", "%M", "-o", str(rss_path), *command]
    completed = subprocess.run(
        timed_command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        child_rss_kib = int(rss_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise SmokeError("child-process RSS measurement was not emitted") from error
    finished_at = datetime.now(UTC)
    is_market_smoke = "scripts/smoke_market_incremental_v2.py" in command
    print(
        f"SMOKE END rc={completed.returncode} "
        f"elapsed_seconds={(perf_counter_ns() - started) / 1_000_000_000:.1f}",
        flush=True,
    )
    result: dict[str, object] = {
        "command": command,
        "started_at": started_at.isoformat(),
        "completed_at": finished_at.isoformat(),
        "elapsed_microseconds": (perf_counter_ns() - started) // 1_000,
        "return_code": completed.returncode,
        "stdout_tail": "" if is_market_smoke else completed.stdout[-16000:],
        "stderr_tail": completed.stderr[-8000:],
        "rss_high_water_bytes": child_rss_kib * 1024,
    }
    report_path = env.get("INVESTMENT_ANALYST_INCREMENTAL_SMOKE_FAMILY_REPORT")
    if report_path is not None:
        try:
            result["family_evidence"] = json.loads(Path(report_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SmokeError("family smoke instrumentation did not emit valid evidence") from error
    if is_market_smoke:
        try:
            result["market_smoke"] = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise SmokeError("market incremental smoke did not emit valid JSON") from error
    return result


def _family_measurement_table(family_results: Mapping[str, object]) -> list[dict[str, object]]:
    """Flatten per-invocation family evidence into the A3 review table."""
    columns = (
        "pipeline",
        "test",
        "test_status",
        "error_type",
        "input_rows_loaded",
        "input_page_count",
        "input_page_size_max",
        "input_record_count",
        "input_ids_sha256",
        "input_id_samples",
        "input_record_keys_sha256",
        "input_content_sha256",
        "input_comparison",
        "input_change_scenario",
        "new_inputs_count",
        "input_revision_count",
        "calculated_candidate_count",
        "candidate_count",
        "candidate_ids_sha256",
        "candidate_id_samples",
        "created_count",
        "created_ids_sha256",
        "created_id_samples",
        "reused_count",
        "reused_ids_sha256",
        "reused_id_samples",
        "event_count",
        "event_ids_sha256",
        "event_id_samples",
        "event_candidate_count",
        "event_candidate_ids_sha256",
        "candidate_lookup_page_count",
        "metric_lookup_page_count",
        "batch_sizes",
        "max_batch_size",
        "repository_query_count",
        "repository_query_counts",
        "input_query_count",
        "input_read_counts",
        "elapsed_microseconds",
        "summary_sha256",
        "content_sha256",
    )
    table: list[dict[str, object]] = []
    for family, result in family_results.items():
        if not isinstance(result, Mapping):
            continue
        evidence = result.get("family_evidence")
        product_runs = evidence.get("product_runs", []) if isinstance(evidence, Mapping) else []
        for run in product_runs:
            if not isinstance(run, Mapping):
                continue
            summary = run.get("summary")
            summary_fields = summary if isinstance(summary, Mapping) else {}
            row = {name: run.get(name) for name in columns}
            row["family"] = family
            for name in (
                "asset_id",
                "manager_cik",
                "source_id",
                "frequency",
                "form",
                "forms",
                "known_at",
                "as_of_from",
                "as_of_before",
            ):
                value = summary_fields.get(name)
                if value is not None:
                    row[name] = value
            table.append(row)
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="new JSON evidence file outside the repository and workspace",
    )
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    output = _output_path(args.output, repository_root)
    head_sha = _git_value(repository_root, "HEAD")
    tree_sha = _git_value(repository_root, "HEAD^{tree}")
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    started_at = datetime.now(UTC)
    overall_started = perf_counter_ns()
    commands: list[dict[str, object]] = []
    family_results: dict[str, dict[str, object]] = {}

    with tempfile.TemporaryDirectory(prefix="investment-analyst-runtime-v2-") as scratch_text:
        scratch = Path(scratch_text).resolve()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    (
                        str(repository_root / "scripts"),
                        str(repository_root / "src"),
                        str(repository_root),
                    )
                ),
                "TMPDIR": str(scratch),
            }
        )
        market_command = [
            sys.executable,
            "scripts/smoke_market_incremental_v2.py",
            "--profile",
            "all",
        ]
        commands.append(
            _run(
                market_command,
                cwd=repository_root,
                env=env,
                started_at=datetime.now(UTC),
            )
        )
        for family, test_paths in _FAMILY_TESTS.items():
            family_report_path = scratch / f"{family}-evidence.json"
            env["INVESTMENT_ANALYST_INCREMENTAL_SMOKE_FAMILY"] = family
            env["INVESTMENT_ANALYST_INCREMENTAL_SMOKE_FAMILY_REPORT"] = str(family_report_path)
            test_command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "smoke_incremental_runtime_v2",
                *test_paths,
            ]
            result = _run(
                test_command,
                cwd=repository_root,
                env=env,
                started_at=datetime.now(UTC),
            )
            result["family"] = family
            result["test_paths"] = list(test_paths)
            family_results[family] = result
            commands.append(result)

    missing_family_pipelines: dict[str, list[str]] = {}
    for family, expected_pipelines in _EXPECTED_FAMILY_PIPELINES.items():
        evidence = family_results[family].get("family_evidence")
        product_runs = evidence.get("product_runs", []) if isinstance(evidence, dict) else []
        rows = [item for item in product_runs if isinstance(item, dict)]
        observed = {str(item.get("pipeline")) for item in rows}
        missing = list(sorted(expected_pipelines - observed))
        for pipeline in sorted(_REQUIRED_POSITIVE_FAMILY_PIPELINES[family]):
            positive = any(
                item.get("pipeline") == pipeline
                and item.get("test_status") == "passed"
                and item.get("error_type") is None
                and (
                    int(item.get("candidate_count", 0)) > 0
                    or int(item.get("event_count", 0)) > 0
                    or int(item.get("event_candidate_count", 0)) > 0
                )
                for item in rows
            )
            if not positive:
                missing.append(f"{pipeline}:no_positive_output")
            oversized = any(
                item.get("pipeline") == pipeline
                and item.get("error_type") is None
                and int(item.get("max_batch_size", 0)) > 256
                for item in rows
            )
            if oversized:
                missing.append(f"{pipeline}:batch_over_256")
        if missing:
            missing_family_pipelines[family] = sorted(missing)
    passed = all(item["return_code"] == 0 for item in commands) and not missing_family_pipelines
    finished_at = datetime.now(UTC)
    document: dict[str, object] = {
        "schema_version": "incremental-runtime-v2-smoke-v1",
        "status": "PASS" if passed else "FAIL",
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "code_sha": None if dirty else head_sha,
        "worktree_dirty": dirty,
        "command": [sys.executable, "scripts/smoke_incremental_runtime_v2.py", *sys.argv[1:]],
        "environment": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "platform": platform.platform(),
        },
        "started_at": started_at.isoformat(),
        "completed_at": finished_at.isoformat(),
        "elapsed_microseconds": (perf_counter_ns() - overall_started) // 1_000,
        "orchestrator_rss_high_water_bytes": _rss_high_water_bytes(),
        "scratch": "exclusive temporary directory outside the repository, removed after exit",
        "families": family_results,
        "family_table": _family_measurement_table(family_results),
        "family_pipeline_gaps": missing_family_pipelines,
        "commands": commands,
    }
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise SmokeError("could not write the smoke evidence file") from error
    print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
