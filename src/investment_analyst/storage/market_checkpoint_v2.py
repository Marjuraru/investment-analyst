"""Append-only persistence for daily EMA/RSI/ATR/MACD recurrence checkpoints."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.market.daily_evidence import DailyEvidencePrefix
from investment_analyst.analytics.market.incremental_state import (
    CheckpointMetricReference,
    MarketRecursiveCheckpoint,
    RecursiveParameters,
)
from investment_analyst.core.interfaces.repositories import BatchWriteReceipt
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.storage.analytical_v2_validation import chunked_sequence
from investment_analyst.storage.bounded_insert import (
    BoundedInsertTable,
    insert_bounded,
    write_transaction,
)
from investment_analyst.storage.daily_evidence_v2 import (
    DailyEvidenceV2Store,
    ensure_daily_evidence_v2_tables,
)
from investment_analyst.storage.errors import RecordConflictError
from investment_analyst.storage.metric_v2 import METRIC_V2_TABLE

MARKET_CHECKPOINT_TABLE = "market_recursive_checkpoints_v2"
CHECKPOINT_METRIC_LINKS_TABLE = "market_recursive_checkpoint_metric_links_v2"
MAX_MARKET_CHECKPOINT_PAGE = 256

_CHECKPOINT_COLUMNS = (
    "checkpoint_id",
    "policy_version",
    "asset_id",
    "source_id",
    "frequency",
    "family",
    "algorithm_version",
    "parameters_json",
    "seed_start",
    "as_of",
    "available_at",
    "daily_prefix_id",
    "daily_prefix_hash",
    "daily_prefix_length",
    "state_json",
)
_CHECKPOINT_LINK_COLUMNS = ("checkpoint_id", "position", "metric_key", "result_id")


class MarketCheckpointV2Error(ValueError):
    """Raised when a checkpoint schema, state or reference cannot be trusted."""


class ExistingMetricLookup(Protocol):
    """Bounded metric lookup needed to verify checkpoint references."""

    def get_existing(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]: ...


def _instant_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketCheckpointV2Error("checkpoint timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise MarketCheckpointV2Error("checkpoint timestamp column is malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MarketCheckpointV2Error("checkpoint timestamp column is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketCheckpointV2Error("checkpoint timestamp column is timezone-naive")
    return parsed.astimezone(UTC)


def _columns(connection: DuckDBPyConnection, table: str) -> set[str]:
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [table],
    ).fetchall()
    return {str(row[0]) for row in rows}


def market_checkpoint_v2_tables_exist(connection: DuckDBPyConnection) -> bool:
    """Return whether either checkpoint table exists, including partial schemas."""
    return bool(
        _columns(connection, MARKET_CHECKPOINT_TABLE)
        or _columns(connection, CHECKPOINT_METRIC_LINKS_TABLE)
    )


def ensure_market_checkpoint_v2_tables(
    connection: DuckDBPyConnection,
    *,
    create: bool,
) -> None:
    """Require the full compatible append-only checkpoint schema."""
    checkpoint_names = _columns(connection, MARKET_CHECKPOINT_TABLE)
    link_names = _columns(connection, CHECKPOINT_METRIC_LINKS_TABLE)
    present = (bool(checkpoint_names), bool(link_names))
    if not any(present):
        if not create:
            raise MarketCheckpointV2Error("market checkpoint v2 tables are missing")
        connection.execute(
            f"""
            CREATE TABLE {MARKET_CHECKPOINT_TABLE} (
                checkpoint_id VARCHAR PRIMARY KEY,
                policy_version VARCHAR NOT NULL,
                asset_id VARCHAR NOT NULL,
                source_id VARCHAR NOT NULL,
                frequency VARCHAR NOT NULL,
                family VARCHAR NOT NULL,
                algorithm_version VARCHAR NOT NULL,
                parameters_json VARCHAR NOT NULL,
                seed_start VARCHAR NOT NULL,
                as_of VARCHAR NOT NULL,
                available_at VARCHAR NOT NULL,
                daily_prefix_id VARCHAR NOT NULL,
                daily_prefix_hash VARCHAR NOT NULL,
                daily_prefix_length INTEGER NOT NULL,
                state_json VARCHAR NOT NULL,
                inserted_at VARCHAR NOT NULL DEFAULT (CAST(CURRENT_TIMESTAMP AS VARCHAR))
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE {CHECKPOINT_METRIC_LINKS_TABLE} (
                checkpoint_id VARCHAR NOT NULL,
                position INTEGER NOT NULL,
                metric_key VARCHAR NOT NULL,
                result_id VARCHAR NOT NULL,
                PRIMARY KEY (checkpoint_id, position),
                UNIQUE (checkpoint_id, metric_key),
                UNIQUE (checkpoint_id, result_id)
            )
            """
        )
        checkpoint_names = _columns(connection, MARKET_CHECKPOINT_TABLE)
        link_names = _columns(connection, CHECKPOINT_METRIC_LINKS_TABLE)
    elif not all(present):
        raise MarketCheckpointV2Error("market checkpoint v2 schema is partially present")
    if checkpoint_names != set((*_CHECKPOINT_COLUMNS, "inserted_at")):
        raise MarketCheckpointV2Error("market checkpoint v2 table is incompatible")
    if link_names != set(_CHECKPOINT_LINK_COLUMNS):
        raise MarketCheckpointV2Error("market checkpoint metric links are incompatible")
    if "document_json" in checkpoint_names or "document_json" in link_names:
        raise MarketCheckpointV2Error("market checkpoint v2 tables must not store documents")


class MarketCheckpointV2Store:
    """Typed append-only checkpoint store and bounded lookup adapter."""

    def __init__(
        self,
        connection: DuckDBPyConnection,
        *,
        metric_results: ExistingMetricLookup | None = None,
    ) -> None:
        self._connection = connection
        self._metric_results = metric_results

    def save_many(
        self,
        checkpoints: Collection[MarketRecursiveCheckpoint],
        *,
        verified_prefixes: Mapping[UUID, DailyEvidencePrefix] | None = None,
        metric_results: ExistingMetricLookup | None = None,
    ) -> BatchWriteReceipt:
        """Persist verified state only after its prefix and referenced metrics exist."""
        ensure_market_checkpoint_v2_tables(self._connection, create=True)
        ensure_daily_evidence_v2_tables(self._connection, create=False)
        ordered = tuple(
            sorted(
                checkpoints,
                key=lambda item: (item.daily_prefix_length, item.as_of, str(item.checkpoint_id)),
            )
        )
        if not ordered:
            return BatchWriteReceipt()
        canonical: dict[UUID, MarketRecursiveCheckpoint] = {}
        for checkpoint in ordered:
            existing = canonical.get(checkpoint.checkpoint_id)
            if existing is not None and not _same_state(existing, checkpoint):
                raise RecordConflictError(
                    f"checkpoint identifier {checkpoint.checkpoint_id} has conflicting state"
                )
            if existing is not None and existing.metric_references != checkpoint.metric_references:
                raise RecordConflictError(
                    f"checkpoint identifier {checkpoint.checkpoint_id} has conflicting references"
                )
            canonical[checkpoint.checkpoint_id] = checkpoint
        unique = tuple(canonical.values())
        if len(unique) > MAX_MARKET_CHECKPOINT_PAGE:
            raise MarketCheckpointV2Error("checkpoint save batch must not exceed 256 states")
        prefix_ids = tuple({item.daily_prefix_id for item in unique})
        prefixes = self._resolve_prefixes(prefix_ids, verified_prefixes)
        if set(prefixes) != set(prefix_ids):
            raise MarketCheckpointV2Error("checkpoint references a missing daily evidence prefix")
        for checkpoint in unique:
            prefix = prefixes[checkpoint.daily_prefix_id]
            if not _prefix_matches(checkpoint, prefix):
                raise MarketCheckpointV2Error("checkpoint and daily evidence prefix diverge")

        metric_store = metric_results or self._metric_results
        existing = self.get_many(
            tuple(canonical),
            verified_prefixes=prefixes,
            metric_results=metric_store,
        )
        created = tuple(item.checkpoint_id for item in unique if item.checkpoint_id not in existing)
        reused = tuple(item for item in canonical if item in existing)
        for checkpoint_id in reused:
            if not _same_state(existing[checkpoint_id], canonical[checkpoint_id]):
                raise RecordConflictError(
                    f"checkpoint identifier {checkpoint_id} already has different state"
                )
        self._verify_metric_references(unique, metric_results=metric_store)

        checkpoint_rows = [
            _checkpoint_row(item) for item in unique if item.checkpoint_id not in existing
        ]
        link_rows = self._new_metric_links(unique, existing)
        if checkpoint_rows or link_rows:
            with write_transaction(self._connection):
                insert_bounded(
                    self._connection,
                    BoundedInsertTable.MARKET_CHECKPOINTS_V2,
                    checkpoint_rows,
                )
                insert_bounded(
                    self._connection,
                    BoundedInsertTable.CHECKPOINT_METRIC_LINKS_V2,
                    link_rows,
                )
        return BatchWriteReceipt(created_ids=created, reused_ids=reused, conflicting_ids=())

    def get_many(
        self,
        checkpoint_ids: Collection[UUID],
        *,
        verified_prefixes: Mapping[UUID, DailyEvidencePrefix] | None = None,
        metric_results: ExistingMetricLookup | None = None,
    ) -> dict[UUID, MarketRecursiveCheckpoint]:
        """Hydrate and verify requested checkpoint state in bounded batches."""
        ensure_market_checkpoint_v2_tables(self._connection, create=False)
        requested = tuple(sorted(set(checkpoint_ids), key=str))
        if not requested:
            return {}
        indexed: dict[UUID, tuple[object, ...]] = {}
        columns = ", ".join(_CHECKPOINT_COLUMNS)
        for chunk in chunked_sequence(requested, MAX_MARKET_CHECKPOINT_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT {columns} FROM {MARKET_CHECKPOINT_TABLE} "
                f"WHERE checkpoint_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            indexed.update({UUID(str(row[0])): row for row in rows})
        if not indexed:
            return {}
        links: dict[UUID, list[tuple[int, CheckpointMetricReference]]] = {
            key: [] for key in indexed
        }
        for chunk in chunked_sequence(tuple(indexed), MAX_MARKET_CHECKPOINT_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT checkpoint_id, position, metric_key, result_id "
                f"FROM {CHECKPOINT_METRIC_LINKS_TABLE} "
                f"WHERE checkpoint_id IN ({placeholders}) ORDER BY checkpoint_id, position",
                [str(item) for item in chunk],
            ).fetchall()
            for checkpoint_id, position, metric_key, result_id in rows:
                key = UUID(str(checkpoint_id))
                if key not in links or isinstance(position, bool) or not isinstance(position, int):
                    raise MarketCheckpointV2Error("checkpoint metric link is malformed")
                links[key].append(
                    (
                        position,
                        CheckpointMetricReference(
                            metric_key=str(metric_key), result_id=UUID(str(result_id))
                        ),
                    )
                )
        output: dict[UUID, MarketRecursiveCheckpoint] = {}
        for checkpoint_id, row in indexed.items():
            ordered_links = links[checkpoint_id]
            if [position for position, _ in ordered_links] != list(range(len(ordered_links))):
                raise MarketCheckpointV2Error("checkpoint metric link positions are invalid")
            if len(row) != len(_CHECKPOINT_COLUMNS):
                raise MarketCheckpointV2Error("checkpoint row is malformed")
            try:
                parameters = json.loads(str(row[7]))
                state = json.loads(str(row[14]))
                checkpoint = MarketRecursiveCheckpoint.model_validate(
                    {
                        "checkpoint_id": checkpoint_id,
                        "policy_version": row[1],
                        "asset_id": row[2],
                        "source_id": row[3],
                        "frequency": row[4],
                        "algorithm_version": row[6],
                        "parameters": parameters,
                        "seed_start": _parse_instant(row[8]),
                        "as_of": _parse_instant(row[9]),
                        "available_at": _parse_instant(row[10]),
                        "daily_prefix_id": row[11],
                        "daily_prefix_hash": row[12],
                        "daily_prefix_length": row[13],
                        "state": state,
                        "metric_references": [item for _, item in ordered_links],
                    }
                )
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                raise MarketCheckpointV2Error("checkpoint row does not validate") from error
            if checkpoint.family != row[5] or checkpoint.checkpoint_id != UUID(str(row[0])):
                raise MarketCheckpointV2Error("checkpoint identity or family diverged")
            output[checkpoint_id] = checkpoint
        prefix_ids = tuple({item.daily_prefix_id for item in output.values()})
        prefixes = self._resolve_prefixes(prefix_ids, verified_prefixes)
        if set(prefixes) != set(prefix_ids):
            raise MarketCheckpointV2Error("checkpoint references a missing daily evidence prefix")
        for checkpoint in output.values():
            if not _prefix_matches(checkpoint, prefixes[checkpoint.daily_prefix_id]):
                raise MarketCheckpointV2Error("checkpoint and daily evidence prefix diverge")
        self._verify_metric_references(
            tuple(output.values()),
            metric_results=metric_results or self._metric_results,
        )
        return output

    def find_for_prefixes(
        self,
        prefix_ids: Collection[UUID],
        parameters: RecursiveParameters,
        *,
        known_at: datetime,
        verified_prefixes: Mapping[UUID, DailyEvidencePrefix] | None = None,
        metric_results: ExistingMetricLookup | None = None,
    ) -> dict[UUID, MarketRecursiveCheckpoint]:
        """Find matching checkpoints for one bounded set of expected prefix IDs."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise MarketCheckpointV2Error("known_at must be timezone-aware")
        ensure_market_checkpoint_v2_tables(self._connection, create=False)
        ids = tuple(sorted(set(prefix_ids), key=str))
        if not ids:
            return {}
        parameters_json = _parameters_text(parameters)
        found: dict[UUID, UUID] = {}
        for chunk in chunked_sequence(ids, MAX_MARKET_CHECKPOINT_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT checkpoint_id, daily_prefix_id FROM {MARKET_CHECKPOINT_TABLE} "
                f"WHERE daily_prefix_id IN ({placeholders}) AND family = ? "
                "AND parameters_json = ? AND available_at <= ? "
                "ORDER BY daily_prefix_length, checkpoint_id",
                [
                    *(str(item) for item in chunk),
                    parameters.family,
                    parameters_json,
                    _instant_text(known_at),
                ],
            ).fetchall()
            for checkpoint_id, prefix_id in rows:
                key = UUID(str(prefix_id))
                if key in found:
                    raise MarketCheckpointV2Error(
                        "multiple checkpoints exist for one prefix and parameter set"
                    )
                found[key] = UUID(str(checkpoint_id))
        loaded = (
            self.get_many(
                tuple(found.values()),
                verified_prefixes=verified_prefixes,
                metric_results=metric_results or self._metric_results,
            )
            if found
            else {}
        )
        return {prefix_id: loaded[checkpoint_id] for prefix_id, checkpoint_id in found.items()}

    def _resolve_prefixes(
        self,
        prefix_ids: Collection[UUID],
        verified_prefixes: Mapping[UUID, DailyEvidencePrefix] | None,
    ) -> dict[UUID, DailyEvidencePrefix]:
        """Reuse validated prefix objects from the current operation, then load the rest."""
        supplied = verified_prefixes or {}
        output: dict[UUID, DailyEvidencePrefix] = {}
        missing: list[UUID] = []
        for prefix_id in sorted(set(prefix_ids), key=str):
            prefix = supplied.get(prefix_id)
            if prefix is None:
                missing.append(prefix_id)
                continue
            if not isinstance(prefix, DailyEvidencePrefix) or prefix.prefix_id != prefix_id:
                raise MarketCheckpointV2Error("verified daily evidence context is inconsistent")
            output[prefix_id] = prefix
        if missing:
            output.update(DailyEvidenceV2Store(self._connection).get_many(missing))
        return output

    def list_ids_page(
        self,
        *,
        limit: int,
        after_checkpoint_id: UUID | None = None,
    ) -> list[UUID]:
        """Return one stable checkpoint page for backup inventory."""
        _validate_page(limit)
        ensure_market_checkpoint_v2_tables(self._connection, create=False)
        where = " WHERE checkpoint_id > ?" if after_checkpoint_id is not None else ""
        parameters: list[object] = (
            [str(after_checkpoint_id)] if after_checkpoint_id is not None else []
        )
        rows = self._connection.execute(
            f"SELECT checkpoint_id FROM {MARKET_CHECKPOINT_TABLE}{where} "
            "ORDER BY checkpoint_id LIMIT ?",
            [*parameters, limit],
        ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def _verify_metric_references(
        self,
        checkpoints: Sequence[MarketRecursiveCheckpoint],
        *,
        metric_results: ExistingMetricLookup | None = None,
    ) -> None:
        references: dict[UUID, list[tuple[MarketRecursiveCheckpoint, str]]] = {}
        for checkpoint in checkpoints:
            for item in checkpoint.metric_references:
                references.setdefault(item.result_id, []).append((checkpoint, item.metric_key))
        if not references:
            return
        if metric_results is not None:
            resolved = {}
            for chunk in chunked_sequence(tuple(references), MAX_MARKET_CHECKPOINT_PAGE):
                resolved.update(metric_results.get_existing(chunk))
            if set(resolved) != set(references):
                raise MarketCheckpointV2Error("checkpoint references a missing metric result")
            for result_id, related_checkpoints in references.items():
                metric = resolved[result_id]
                parameters = metric.parameters
                for checkpoint, metric_key in related_checkpoints:
                    if (
                        metric.result_id != result_id
                        or metric.asset_id != checkpoint.asset_id
                        or metric.metric_key != metric_key
                        or metric.as_of != checkpoint.as_of
                        or metric.available_at > checkpoint.available_at
                        or parameters.get("daily_evidence_prefix_id")
                        != str(checkpoint.daily_prefix_id)
                        or parameters.get("market_checkpoint_id") != str(checkpoint.checkpoint_id)
                    ):
                        raise MarketCheckpointV2Error(
                            "checkpoint metric reference is outside its prefix"
                        )
            return
        metrics: dict[UUID, tuple[object, ...]] = {}
        for chunk in chunked_sequence(tuple(references), MAX_MARKET_CHECKPOINT_PAGE):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"SELECT result_id, asset_id, metric_key, as_of, available_at, parameters_json "
                f"FROM {METRIC_V2_TABLE} WHERE result_id IN ({placeholders})",
                [str(item) for item in chunk],
            ).fetchall()
            metrics.update({UUID(str(row[0])): row for row in rows})
        if set(metrics) != set(references):
            raise MarketCheckpointV2Error("checkpoint references a missing metric result")
        for result_id, related_checkpoints in references.items():
            row = metrics[result_id]
            try:
                metric_parameters = json.loads(str(row[5]))
            except json.JSONDecodeError as error:
                raise MarketCheckpointV2Error(
                    "checkpoint metric parameters are malformed"
                ) from error
            if not isinstance(metric_parameters, dict):
                raise MarketCheckpointV2Error("checkpoint metric parameters are malformed")
            for checkpoint, metric_key in related_checkpoints:
                if (
                    row[1] != checkpoint.asset_id
                    or row[2] != metric_key
                    or _parse_instant(row[3]) != checkpoint.as_of
                    or _parse_instant(row[4]) > checkpoint.available_at
                    or metric_parameters.get("daily_evidence_prefix_id")
                    != str(checkpoint.daily_prefix_id)
                    or metric_parameters.get("market_checkpoint_id")
                    != str(checkpoint.checkpoint_id)
                ):
                    raise MarketCheckpointV2Error(
                        "checkpoint metric reference is outside its prefix"
                    )

    def _new_metric_links(
        self,
        checkpoints: Sequence[MarketRecursiveCheckpoint],
        existing: dict[UUID, MarketRecursiveCheckpoint],
    ) -> list[list[object]]:
        links: list[list[object]] = []
        for checkpoint in checkpoints:
            previous = existing.get(checkpoint.checkpoint_id)
            known = (
                {item.metric_key: item.result_id for item in previous.metric_references}
                if previous is not None
                else {}
            )
            desired = {item.metric_key: item.result_id for item in checkpoint.metric_references}
            for metric_key, result_id in known.items():
                if metric_key in desired and desired[metric_key] != result_id:
                    raise RecordConflictError(
                        f"checkpoint {checkpoint.checkpoint_id} has a conflicting metric reference"
                    )
            additions = [(key, value) for key, value in desired.items() if key not in known]
            start = len(known)
            links.extend(
                [str(checkpoint.checkpoint_id), start + index, key, str(result_id)]
                for index, (key, result_id) in enumerate(sorted(additions))
            )
        return links


def _checkpoint_row(checkpoint: MarketRecursiveCheckpoint) -> list[object]:
    return [
        str(checkpoint.checkpoint_id),
        checkpoint.policy_version,
        checkpoint.asset_id,
        checkpoint.source_id,
        checkpoint.frequency.value,
        checkpoint.family,
        checkpoint.algorithm_version,
        _parameters_text(checkpoint.parameters),
        _instant_text(checkpoint.seed_start),
        _instant_text(checkpoint.as_of),
        _instant_text(checkpoint.available_at),
        str(checkpoint.daily_prefix_id),
        checkpoint.daily_prefix_hash,
        checkpoint.daily_prefix_length,
        _state_text(checkpoint),
    ]


def _parameters_text(parameters: RecursiveParameters) -> str:
    return json.dumps(
        parameters.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _state_text(checkpoint: MarketRecursiveCheckpoint) -> str:
    return json.dumps(
        checkpoint.state.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _same_state(
    left: MarketRecursiveCheckpoint,
    right: MarketRecursiveCheckpoint,
) -> bool:
    return left.model_dump(mode="python", exclude={"metric_references"}) == right.model_dump(
        mode="python", exclude={"metric_references"}
    )


def _prefix_matches(
    checkpoint: MarketRecursiveCheckpoint,
    prefix: DailyEvidencePrefix,
) -> bool:
    return (
        checkpoint.daily_prefix_id == prefix.prefix_id
        and checkpoint.daily_prefix_hash == prefix.prefix_hash
        and checkpoint.daily_prefix_length == prefix.length
        and checkpoint.asset_id == prefix.asset_id
        and checkpoint.source_id == prefix.source_id
        and checkpoint.as_of == prefix.timestamp
        and checkpoint.available_at == prefix.available_at
    )


def _validate_page(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise MarketCheckpointV2Error("checkpoint page limit must be an integer")
    if limit < 1 or limit > MAX_MARKET_CHECKPOINT_PAGE:
        raise MarketCheckpointV2Error("checkpoint page limit must be between 1 and 256")


__all__ = [
    "CHECKPOINT_METRIC_LINKS_TABLE",
    "MARKET_CHECKPOINT_TABLE",
    "MAX_MARKET_CHECKPOINT_PAGE",
    "MarketCheckpointV2Error",
    "MarketCheckpointV2Store",
    "ensure_market_checkpoint_v2_tables",
    "market_checkpoint_v2_tables_exist",
]
