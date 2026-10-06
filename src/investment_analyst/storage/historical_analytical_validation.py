"""Bounded relational validation for the historical analytical archive."""

from __future__ import annotations

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchiveError,
    ensure_historical_analytical_archive_tables,
)

_PAGE = 256


class HistoricalAnalyticalValidationSummary(ContractModel):
    """Counts and proof digests from one complete archive validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    complete: bool
    metric_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)
    metric_observation_links: int = Field(ge=0)
    metric_metric_links: int = Field(ge=0)
    diagnostic_component_links: int = Field(ge=0)
    diagnostic_evidence_links: int = Field(ge=0)
    sequence_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


def _count(connection: DuckDBPyConnection, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    if row is None:
        raise HistoricalAnalyticalArchiveError("historical analytical inventory is unavailable")
    return int(row[0])


def _require_zero(
    connection: DuckDBPyConnection, sql: str, parameters: list[object], message: str
) -> None:
    row = connection.execute(sql, parameters).fetchone()
    if row is None or int(row[0]) != 0:
        raise HistoricalAnalyticalArchiveError(message)


def _validate_reference_rows(connection: DuckDBPyConnection) -> None:
    metric_observation = connection.execute(
        """SELECT count(*)
           FROM historical_metric_results AS metric
           JOIN historical_analytical_sequences AS sequence
             ON sequence.sequence_id = metric.observation_sequence_id
           JOIN historical_analytical_sequence_segments AS sequence_segment
             ON sequence_segment.sequence_id = sequence.sequence_id
           JOIN historical_analytical_segments AS segment
             ON segment.segment_id = sequence_segment.segment_id
           JOIN historical_analytical_segment_members AS member
             ON member.segment_id = segment.segment_id
           LEFT JOIN normalized_observations_v2 AS observation
             ON observation.observation_id = member.identifier
           WHERE sequence.link_type <> 'metric-input-observation-v1'
              OR segment.link_type <> sequence.link_type
              OR observation.observation_id IS NULL
              OR observation.asset_id <> metric.asset_id
              OR observation.available_at > metric.available_at"""
    ).fetchone()
    if metric_observation is None or int(metric_observation[0]) != 0:
        raise HistoricalAnalyticalArchiveError(
            "historical metric observation references are missing, foreign, or future"
        )
    metric_metric = connection.execute(
        """SELECT count(*)
           FROM historical_metric_results AS metric
           JOIN historical_analytical_sequences AS sequence
             ON sequence.sequence_id = metric.metric_sequence_id
           JOIN historical_analytical_sequence_segments AS sequence_segment
             ON sequence_segment.sequence_id = sequence.sequence_id
           JOIN historical_analytical_segments AS segment
             ON segment.segment_id = sequence_segment.segment_id
           JOIN historical_analytical_segment_members AS member
             ON member.segment_id = segment.segment_id
           LEFT JOIN historical_metric_results AS input_metric
             ON input_metric.result_id = member.identifier
           WHERE sequence.link_type <> 'metric-input-result-v1'
              OR segment.link_type <> sequence.link_type
              OR input_metric.result_id IS NULL
              OR input_metric.asset_id <> metric.asset_id
              OR input_metric.available_at > metric.available_at"""
    ).fetchone()
    if metric_metric is None or int(metric_metric[0]) != 0:
        raise HistoricalAnalyticalArchiveError(
            "historical metric dependencies are missing, foreign, or future"
        )
    component_refs = connection.execute(
        """SELECT count(*)
           FROM historical_analytical_components AS component
           JOIN historical_diagnostic_results AS diagnostic
             ON diagnostic.diagnostic_id = component.diagnostic_id
           JOIN historical_analytical_sequences AS sequence
             ON sequence.sequence_id = component.metric_sequence_id
           JOIN historical_analytical_sequence_segments AS sequence_segment
             ON sequence_segment.sequence_id = sequence.sequence_id
           JOIN historical_analytical_segments AS segment
             ON segment.segment_id = sequence_segment.segment_id
           JOIN historical_analytical_segment_members AS member
             ON member.segment_id = segment.segment_id
           LEFT JOIN historical_metric_results AS metric
             ON metric.result_id = member.identifier
           WHERE sequence.link_type <> 'diagnostic-component-metric-result-v1'
              OR segment.link_type <> sequence.link_type
              OR metric.result_id IS NULL
              OR metric.asset_id <> diagnostic.asset_id
              OR metric.available_at > diagnostic.available_at"""
    ).fetchone()
    if component_refs is None or int(component_refs[0]) != 0:
        raise HistoricalAnalyticalArchiveError(
            "historical diagnostic component references are missing, foreign, or future"
        )
    evidence_refs = connection.execute(
        """SELECT count(*)
           FROM historical_analytical_evidence AS evidence
           JOIN historical_diagnostic_results AS diagnostic
             ON diagnostic.diagnostic_id = evidence.diagnostic_id
           LEFT JOIN historical_metric_results AS metric
             ON metric.result_id = evidence.metric_result_id
           WHERE metric.result_id IS NULL
              OR metric.asset_id <> diagnostic.asset_id
              OR metric.available_at > diagnostic.available_at"""
    ).fetchone()
    if evidence_refs is None or int(evidence_refs[0]) != 0:
        raise HistoricalAnalyticalArchiveError(
            "historical diagnostic evidence is missing, foreign, or future"
        )


def _validate_metric_acyclic(connection: DuckDBPyConnection) -> None:
    connection.execute("DROP TABLE IF EXISTS tmp_historical_metric_nodes")
    connection.execute("DROP TABLE IF EXISTS tmp_historical_metric_edges")
    try:
        connection.execute(
            """CREATE TEMP TABLE tmp_historical_metric_edges AS
               SELECT metric.result_id AS parent_id, member.identifier AS input_id
               FROM historical_metric_results AS metric
               JOIN historical_analytical_sequences AS sequence
                 ON sequence.sequence_id = metric.metric_sequence_id
               JOIN historical_analytical_sequence_segments AS sequence_segment
                 ON sequence_segment.sequence_id = sequence.sequence_id
               JOIN historical_analytical_segment_members AS member
                 ON member.segment_id = sequence_segment.segment_id"""
        )
        connection.execute(
            """CREATE TEMP TABLE tmp_historical_metric_nodes AS
               SELECT metric.result_id,
                      count(edge.input_id)::BIGINT AS remaining_dependencies
               FROM historical_metric_results AS metric
               LEFT JOIN tmp_historical_metric_edges AS edge
                 ON edge.parent_id = metric.result_id
               GROUP BY metric.result_id"""
        )
        remaining = _count(connection, "tmp_historical_metric_nodes")
        while remaining:
            ready = connection.execute(
                "SELECT result_id FROM tmp_historical_metric_nodes "
                "WHERE remaining_dependencies = 0 ORDER BY result_id LIMIT 256"
            ).fetchall()
            if not ready:
                raise HistoricalAnalyticalArchiveError(
                    "historical metric dependency graph contains a cycle"
                )
            identifiers = [str(row[0]) for row in ready]
            placeholders = ",".join("?" for _ in identifiers)
            connection.execute(
                "UPDATE tmp_historical_metric_nodes AS parent SET remaining_dependencies = "
                "parent.remaining_dependencies - dependencies.dependency_count "
                "FROM (SELECT parent_id, count(*)::BIGINT AS dependency_count "
                "FROM tmp_historical_metric_edges WHERE input_id IN (" + placeholders + ") "
                "GROUP BY parent_id) AS dependencies "
                "WHERE parent.result_id = dependencies.parent_id",
                identifiers,
            )
            connection.execute(
                f"DELETE FROM tmp_historical_metric_nodes WHERE result_id IN ({placeholders})",
                identifiers,
            )
            remaining -= len(identifiers)
    finally:
        connection.execute("DROP TABLE IF EXISTS tmp_historical_metric_edges")
        connection.execute("DROP TABLE IF EXISTS tmp_historical_metric_nodes")


def _validate_archive_sequences(connection: DuckDBPyConnection) -> None:
    _require_zero(
        connection,
        """SELECT count(*) FROM historical_analytical_sequences AS sequence
           WHERE NOT EXISTS (SELECT 1 FROM historical_metric_results AS metric
                             WHERE metric.observation_sequence_id = sequence.sequence_id
                                OR metric.metric_sequence_id = sequence.sequence_id)
             AND NOT EXISTS (SELECT 1 FROM historical_analytical_components AS component
                             WHERE component.metric_sequence_id = sequence.sequence_id)""",
        [],
        "historical analytical archive has an unreferenced sequence",
    )
    _require_zero(
        connection,
        """SELECT count(*) FROM historical_analytical_segments AS segment
           WHERE NOT EXISTS (SELECT 1 FROM historical_analytical_sequence_segments AS reference
                             WHERE reference.segment_id = segment.segment_id)""",
        [],
        "historical analytical archive has an unreferenced segment",
    )
    _require_zero(
        connection,
        """SELECT count(*) FROM historical_analytical_segment_members AS member
           LEFT JOIN historical_analytical_segments AS segment
             ON segment.segment_id = member.segment_id
           WHERE segment.segment_id IS NULL""",
        [],
        "historical analytical archive has a segment member without a segment",
    )
    _require_zero(
        connection,
        """SELECT count(*) FROM historical_analytical_sequence_segments AS reference
           LEFT JOIN historical_analytical_sequences AS sequence
             ON sequence.sequence_id = reference.sequence_id
           LEFT JOIN historical_analytical_segments AS segment
             ON segment.segment_id = reference.segment_id
           WHERE sequence.sequence_id IS NULL OR segment.segment_id IS NULL
              OR sequence.link_type <> segment.link_type""",
        [],
        "historical analytical sequence links are incomplete or incompatible",
    )


def verify_historical_analytical_archive(
    connection: DuckDBPyConnection,
) -> HistoricalAnalyticalValidationSummary:
    """Verify complete typed references, point-in-time links, and acyclic dependencies."""
    ensure_historical_analytical_archive_tables(connection, create=False)
    try:
        verify_historical_analytical_archive_structure(connection)
        observation_links = connection.execute(
            """SELECT coalesce(sum(item_count), 0) FROM historical_metric_results AS metric
               JOIN historical_analytical_sequences AS sequence
                 ON sequence.sequence_id = metric.observation_sequence_id
               WHERE sequence.link_type = 'metric-input-observation-v1'"""
        ).fetchone()
        metric_links = connection.execute(
            """SELECT coalesce(sum(item_count), 0) FROM historical_metric_results AS metric
               JOIN historical_analytical_sequences AS sequence
                 ON sequence.sequence_id = metric.metric_sequence_id
               WHERE sequence.link_type = 'metric-input-result-v1'"""
        ).fetchone()
        component_links = connection.execute(
            """SELECT coalesce(sum(sequence.item_count), 0)
               FROM historical_analytical_components AS component
               JOIN historical_analytical_sequences AS sequence
                 ON sequence.sequence_id = component.metric_sequence_id"""
        ).fetchone()
        if observation_links is None or metric_links is None or component_links is None:
            raise HistoricalAnalyticalArchiveError(
                "historical analytical link inventory is unavailable"
            )
        _validate_reference_rows(connection)
        _validate_metric_acyclic(connection)
        return HistoricalAnalyticalValidationSummary(
            complete=True,
            metric_count=_count(connection, "historical_metric_results"),
            diagnostic_count=_count(connection, "historical_diagnostic_results"),
            metric_observation_links=int(observation_links[0]),
            metric_metric_links=int(metric_links[0]),
            diagnostic_component_links=int(component_links[0]),
            diagnostic_evidence_links=_count(connection, "historical_analytical_evidence"),
            sequence_count=_count(connection, "historical_analytical_sequences"),
            segment_count=_count(connection, "historical_analytical_segments"),
        )
    except HistoricalAnalyticalArchiveError:
        raise
    except Exception as error:
        raise HistoricalAnalyticalArchiveError(
            "historical analytical archive graph could not be verified"
        ) from error


def verify_historical_analytical_archive_structure(connection: DuckDBPyConnection) -> None:
    """Verify sequence/segment ownership without requiring future graph targets."""
    ensure_historical_analytical_archive_tables(connection, create=False)
    try:
        _validate_archive_sequences(connection)
    except HistoricalAnalyticalArchiveError:
        raise
    except Exception as error:
        raise HistoricalAnalyticalArchiveError(
            "historical analytical archive structure could not be verified"
        ) from error


__all__ = [
    "HistoricalAnalyticalValidationSummary",
    "verify_historical_analytical_archive",
    "verify_historical_analytical_archive_structure",
]
