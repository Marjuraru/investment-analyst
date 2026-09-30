"""Shared analytical v2 integrity, domain and bounded batch validation helpers.

Part of DATA-CHASSIS-34.
Implements:
- Bounded chunking (<= 256 items) for ID lookups and queries.
- Keyset cursor validation and tie-breaking paging.
- Strict contiguous link position verification (0..N-1).
- Batch metric dependency graph verification (cycle detection, asset matching,
  availability checks, iterative topological ordering avoiding recursion).
- Diagnostic component, link, and evidence verification with domain consistency.
- Snapshot domain validation, direct and diagnostic cited metric resolution,
  and comprehensive EvidenceSet digest verification.
- Chunked, bounded batch hydration preventing N+1 queries.
"""

from __future__ import annotations

import collections
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from duckdb import DuckDBPyConnection

from investment_analyst.analytics.analysis_domain import (
    require_authorized_domain,
    validate_diagnostic_internal_consistency,
    validate_metric_key_for_domain,
)
from investment_analyst.analytics.analysis_snapshot import (
    AnalysisSnapshot,
    canonical_evidence_set_digest,
)
from investment_analyst.core.models.diagnostic import (
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticResult,
)
from investment_analyst.core.models.enums import (
    DataQuality,
    DiagnosticMode,
    DiagnosticVerdict,
    EvidenceDirection,
)
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.storage.errors import (
    RecordNotFoundError,
    StorageError,
)

MAX_CHUNK_SIZE: Final[int] = 256
_METRIC_TABLE = "metric_results_v2"
_METRIC_OBS_LINKS = "metric_v2_observation_links"
_METRIC_METRIC_LINKS = "metric_v2_metric_links"
_DIAG_TABLE = "diagnostic_results_v2"
_DIAG_COMPS = "diagnostic_v2_components"
_DIAG_COMP_LINKS = "diagnostic_v2_component_metric_links"
_DIAG_EVIDENCE = "diagnostic_v2_evidence"
_SNAP_TABLE = "analysis_snapshots_v2"
_SNAP_METRIC_LINKS = "analysis_snapshot_v2_metric_links"
_SNAP_DIAG_LINKS = "analysis_snapshot_v2_diagnostic_links"
_OBS_TABLE = "normalized_observations_v2"
_ES_TABLE = "evidence_sets_v2"


class AnalyticalV2ValidationError(StorageError):
    """Raised when analytical v2 integrity, references or domains fail verification."""


def validate_keyset_limit(limit: int) -> int:
    """Validate that limit is an integer strictly between 1 and 256."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AnalyticalV2ValidationError(
            f"keyset limit must be an integer, got {type(limit).__name__}"
        )
    if limit < 1 or limit > MAX_CHUNK_SIZE:
        raise AnalyticalV2ValidationError(
            f"keyset limit must be between 1 and {MAX_CHUNK_SIZE}, got {limit}"
        )
    return limit


def validate_keyset_cursor(
    cursor_at: datetime | str | None,
    cursor_id: UUID | str | None,
) -> tuple[str, str] | None:
    """Validate keyset cursor coordinates, returning normalized UTC instant and ID strings.

    Rejects partial cursors (one provided, one None).
    """
    if cursor_at is None and cursor_id is None:
        return None
    if cursor_at is None or cursor_id is None:
        raise AnalyticalV2ValidationError(
            "keyset cursor must specify both cursor_at and cursor_id, or neither"
        )

    if isinstance(cursor_at, datetime):
        if cursor_at.tzinfo is None or cursor_at.utcoffset() is None:
            raise AnalyticalV2ValidationError("cursor_at must be timezone-aware")
        at_str = cursor_at.astimezone(UTC).isoformat()
    elif isinstance(cursor_at, str):
        parsed = datetime.fromisoformat(cursor_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AnalyticalV2ValidationError("cursor_at must be timezone-aware")
        at_str = parsed.astimezone(UTC).isoformat()
    else:
        raise AnalyticalV2ValidationError("cursor_at must be a datetime or ISO string")

    id_str = str(cursor_id).strip()
    if not id_str:
        raise AnalyticalV2ValidationError("cursor_id cannot be empty")
    return at_str, id_str


def validate_link_positions(positions: Sequence[int], label: str = "links") -> None:
    """Validate that positions form an unbroken 0..N-1 contiguous sequence."""
    expected = tuple(range(len(positions)))
    if tuple(positions) != expected:
        raise AnalyticalV2ValidationError(
            f"{label} positions are corrupt, gapped, or out of order: "
            f"got {positions!r}, expected {expected!r}"
        )


def chunked_sequence[T](items: Sequence[T], chunk_size: int = MAX_CHUNK_SIZE) -> list[Sequence[T]]:
    """Split a sequence into slices of at most chunk_size."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


def parse_instant_utc(value: object, label: str = "instant") -> datetime:
    """Parse text into a timezone-aware UTC datetime."""
    if value is None:
        raise AnalyticalV2ValidationError(f"{label} is missing")
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticalV2ValidationError(f"{label} is not timezone-aware")
    return parsed.astimezone(UTC)


def instant_to_text(value: datetime) -> str:
    """Format datetime as normalized UTC ISO text."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalyticalV2ValidationError("instant must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def parse_decimal(value: object, label: str = "decimal") -> Decimal:
    """Parse a Decimal strictly and ensure it is finite."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise AnalyticalV2ValidationError(f"{label} is not a valid Decimal") from error
    if not parsed.is_finite():
        raise AnalyticalV2ValidationError(f"{label} must be finite")
    return parsed


def topological_sort_metrics(results: Sequence[MetricResult]) -> list[MetricResult]:
    """Topologically sort a batch of MetricResults iteratively avoiding recursion.

    Detects self-references and cycles within the batch.
    """
    by_id = {str(m.result_id): m for m in results}
    in_degree: dict[str, int] = {k: 0 for k in by_id}
    dependents: dict[str, list[str]] = collections.defaultdict(list)

    for mid_str, metric in by_id.items():
        seen_deps: set[str] = set()
        for dep in metric.input_metric_result_ids:
            dep_str = str(dep)
            if dep_str in seen_deps:
                raise AnalyticalV2ValidationError(
                    f"metric {mid_str} contains duplicate metric input {dep_str}"
                )
            seen_deps.add(dep_str)
            if dep_str == mid_str:
                raise AnalyticalV2ValidationError(
                    f"metric dependency cycle: {mid_str} references itself"
                )
            if dep_str in by_id:
                in_degree[mid_str] += 1
                dependents[dep_str].append(mid_str)

    queue = collections.deque([mid_str for mid_str, degree in in_degree.items() if degree == 0])
    sorted_ids: list[str] = []

    while queue:
        current = queue.popleft()
        sorted_ids.append(current)
        for dep in dependents[current]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    if len(sorted_ids) != len(by_id):
        cyclic_ids = sorted(k for k, degree in in_degree.items() if degree > 0)
        raise AnalyticalV2ValidationError(
            f"metric dependency cycle detected among metrics: {cyclic_ids}"
        )

    return [by_id[mid_str] for mid_str in sorted_ids]


def fetch_metrics_chunked(
    connection: DuckDBPyConnection,
    result_ids: Collection[UUID],
) -> dict[UUID, MetricResult]:
    """Hydrate MetricResult models in chunks of <= 256 without N+1 queries."""
    from investment_analyst.storage.metric_v2 import row_to_metric

    ordered_ids = tuple(sorted(set(result_ids), key=str))
    if not ordered_ids:
        return {}

    results: dict[UUID, MetricResult] = {}
    id_chunks = chunked_sequence(ordered_ids, MAX_CHUNK_SIZE)

    for chunk in id_chunks:
        chunk_str = [str(item) for item in chunk]
        placeholders = ", ".join("?" for _ in chunk)

        # 1. Metric rows
        rows = connection.execute(
            f"SELECT result_id, asset_id, metric_key, value_text, unit, as_of, "
            f"available_at, computed_at, parameters_json, evidence_set_id, "
            f"algorithm_version, quality "
            f"FROM {_METRIC_TABLE} WHERE result_id IN ({placeholders})",
            chunk_str,
        ).fetchall()

        rows_by_id = {str(row[0]): row for row in rows}
        for mid in chunk:
            if str(mid) not in rows_by_id:
                raise RecordNotFoundError(f"metric v2 {mid} was not found")

        # 2. Observation links
        obs_rows = connection.execute(
            f"SELECT result_id, position, observation_id "
            f"FROM {_METRIC_OBS_LINKS} WHERE result_id IN ({placeholders}) "
            "ORDER BY result_id, position",
            chunk_str,
        ).fetchall()
        obs_by_metric: dict[str, list[tuple[int, UUID]]] = collections.defaultdict(list)
        for r_id, pos, obs_id in obs_rows:
            obs_by_metric[str(r_id)].append((int(pos), UUID(str(obs_id))))

        # 3. Metric links
        met_rows = connection.execute(
            f"SELECT result_id, position, input_result_id "
            f"FROM {_METRIC_METRIC_LINKS} WHERE result_id IN ({placeholders}) "
            "ORDER BY result_id, position",
            chunk_str,
        ).fetchall()
        met_by_metric: dict[str, list[tuple[int, UUID]]] = collections.defaultdict(list)
        for r_id, pos, dep_id in met_rows:
            met_by_metric[str(r_id)].append((int(pos), UUID(str(dep_id))))

        for mid in chunk:
            mid_str = str(mid)
            row = rows_by_id[mid_str]

            obs_pairs = obs_by_metric.get(mid_str, [])
            validate_link_positions([p[0] for p in obs_pairs], "metric observation links")
            obs_ids = [p[1] for p in obs_pairs]

            met_pairs = met_by_metric.get(mid_str, [])
            validate_link_positions([p[0] for p in met_pairs], "metric dependency links")
            dep_ids = [p[1] for p in met_pairs]

            results[mid] = row_to_metric(row, observation_ids=obs_ids, metric_ids=dep_ids)

    return results


def fetch_diagnostics_chunked(
    connection: DuckDBPyConnection,
    diagnostic_ids: Collection[UUID],
) -> dict[UUID, DiagnosticResult]:
    """Hydrate DiagnosticResult models in chunks of <= 256 without N+1 queries.

    Verifies all cited metrics, check positions, active matching and domain consistency.
    """
    ordered_ids = tuple(sorted(set(diagnostic_ids), key=str))
    if not ordered_ids:
        return {}

    results: dict[UUID, DiagnosticResult] = {}
    id_chunks = chunked_sequence(ordered_ids, MAX_CHUNK_SIZE)

    for chunk in id_chunks:
        chunk_str = [str(item) for item in chunk]
        placeholders = ", ".join("?" for _ in chunk)

        # 1. Diagnostic rows
        rows = connection.execute(
            f"SELECT diagnostic_id, asset_id, mode, verdict, final_score_text, "
            f"confidence_text, as_of, available_at, computed_at, algorithm_version, "
            f"summary, quality "
            f"FROM {_DIAG_TABLE} WHERE diagnostic_id IN ({placeholders})",
            chunk_str,
        ).fetchall()

        rows_by_id = {str(row[0]): row for row in rows}
        for did in chunk:
            if str(did) not in rows_by_id:
                raise RecordNotFoundError(f"diagnostic result {did} not found")

        # 2. Components
        comp_rows = connection.execute(
            f"SELECT diagnostic_id, position, component_key, score_text, weight_text, "
            f"weighted_contribution_text, explanation "
            f"FROM {_DIAG_COMPS} WHERE diagnostic_id IN ({placeholders}) "
            "ORDER BY diagnostic_id, position",
            chunk_str,
        ).fetchall()
        comps_by_diag: dict[str, list[tuple[int, str, Decimal, Decimal, Decimal, str]]] = (
            collections.defaultdict(list)
        )
        for d_id, pos, key, score, weight, contrib, expl in comp_rows:
            comps_by_diag[str(d_id)].append(
                (
                    int(pos),
                    str(key),
                    parse_decimal(score, "component score"),
                    parse_decimal(weight, "component weight"),
                    parse_decimal(contrib, "component contribution"),
                    str(expl),
                )
            )

        # 3. Component metric links
        comp_link_rows = connection.execute(
            f"SELECT diagnostic_id, component_position, link_position, metric_result_id "
            f"FROM {_DIAG_COMP_LINKS} WHERE diagnostic_id IN ({placeholders}) "
            "ORDER BY diagnostic_id, component_position, link_position",
            chunk_str,
        ).fetchall()
        comp_links: dict[str, dict[int, list[tuple[int, UUID]]]] = collections.defaultdict(
            lambda: collections.defaultdict(list)
        )
        for d_id, c_pos, l_pos, mid in comp_link_rows:
            comp_links[str(d_id)][int(c_pos)].append((int(l_pos), UUID(str(mid))))

        # 4. Evidence
        ev_rows = connection.execute(
            f"SELECT diagnostic_id, position, metric_result_id, direction, "
            f"contribution_text, reason "
            f"FROM {_DIAG_EVIDENCE} WHERE diagnostic_id IN ({placeholders}) "
            "ORDER BY diagnostic_id, position",
            chunk_str,
        ).fetchall()
        ev_by_diag: dict[str, list[tuple[int, UUID, str, Decimal, str]]] = collections.defaultdict(
            list
        )
        for d_id, pos, mid, dir_text, contrib, reason in ev_rows:
            ev_by_diag[str(d_id)].append(
                (
                    int(pos),
                    UUID(str(mid)),
                    str(dir_text),
                    parse_decimal(contrib, "evidence contribution"),
                    str(reason),
                )
            )

        # Collect all cited metric IDs across this chunk
        cited_metric_ids: set[UUID] = set()
        for _diag_id_key, c_pos_dict in comp_links.items():
            for _c_pos_key, link_list in c_pos_dict.items():
                for _, mid in link_list:
                    cited_metric_ids.add(mid)
        for _diag_id_key, ev_list in ev_by_diag.items():
            for _, mid, _, _, _ in ev_list:
                cited_metric_ids.add(mid)

        # Verify cited metrics in chunks <= 256
        cited_metric_info: dict[UUID, tuple[str, datetime, str]] = {}
        for m_chunk in chunked_sequence(sorted(cited_metric_ids, key=str), MAX_CHUNK_SIZE):
            m_placeholders = ", ".join("?" for _ in m_chunk)
            m_rows = connection.execute(
                f"SELECT result_id, asset_id, available_at, metric_key "
                f"FROM {_METRIC_TABLE} WHERE result_id IN ({m_placeholders})",
                [str(item) for item in m_chunk],
            ).fetchall()
            for r_id, a_id, avail, key in m_rows:
                cited_metric_info[UUID(str(r_id))] = (
                    str(a_id),
                    parse_instant_utc(avail, "metric available_at"),
                    str(key),
                )

        # Check all cited metrics exist
        for mid in cited_metric_ids:
            if mid not in cited_metric_info:
                raise RecordNotFoundError(f"diagnostic references missing metric {mid}")

        # Assemble diagnostics
        for did in chunk:
            did_str = str(did)
            row = rows_by_id[did_str]
            asset_id = str(row[1])
            mode = DiagnosticMode(str(row[2]))
            verdict = DiagnosticVerdict(str(row[3]))
            final_score = parse_decimal(row[4], "diagnostic final_score")
            confidence = parse_decimal(row[5], "diagnostic confidence")
            as_of = parse_instant_utc(row[6], "diagnostic as_of")
            available_at = parse_instant_utc(row[7], "diagnostic available_at")
            computed_at = parse_instant_utc(row[8], "diagnostic computed_at")
            algorithm_version = str(row[9])
            summary = str(row[10])
            quality = DataQuality(str(row[11]))

            # Validate components
            c_list = comps_by_diag.get(did_str, [])
            validate_link_positions([c[0] for c in c_list], "diagnostic components")
            components: list[DiagnosticComponent] = []
            for pos, c_key, score, weight, contrib, expl in c_list:
                comp_link_items = comp_links.get(did_str, {}).get(pos, [])
                validate_link_positions(
                    [item[0] for item in comp_link_items], "component metric links"
                )
                comp_mids = [item[1] for item in comp_link_items]
                for mid in comp_mids:
                    m_asset, m_avail, _ = cited_metric_info[mid]
                    if m_asset != asset_id:
                        raise AnalyticalV2ValidationError(
                            f"diagnostic for {asset_id} references foreign metric {mid} "
                            f"belonging to {m_asset}"
                        )
                    if m_avail > available_at:
                        raise AnalyticalV2ValidationError(
                            f"diagnostic available at {available_at.isoformat()} references future "
                            f"metric {mid} available at {m_avail.isoformat()}"
                        )
                components.append(
                    DiagnosticComponent(
                        component_key=c_key,
                        score=score,
                        weight=weight,
                        weighted_contribution=contrib,
                        metric_result_ids=comp_mids,
                        explanation=expl,
                    )
                )

            # Validate evidence
            e_list = ev_by_diag.get(did_str, [])
            validate_link_positions([e[0] for e in e_list], "diagnostic evidence")
            evidence: list[DiagnosticEvidence] = []
            for _ev_pos, mid, dir_text, contrib, reason in e_list:
                m_asset, m_avail, _ = cited_metric_info[mid]
                if m_asset != asset_id:
                    raise AnalyticalV2ValidationError(
                        f"diagnostic for {asset_id} references foreign metric {mid} "
                        f"belonging to {m_asset}"
                    )
                if m_avail > available_at:
                    raise AnalyticalV2ValidationError(
                        f"diagnostic available at {available_at.isoformat()} references future "
                        f"metric {mid} available at {m_avail.isoformat()}"
                    )
                evidence.append(
                    DiagnosticEvidence(
                        metric_result_id=mid,
                        direction=EvidenceDirection(dir_text),
                        contribution=contrib,
                        reason=reason,
                    )
                )

            diag = DiagnosticResult(
                diagnostic_id=did,
                asset_id=asset_id,
                mode=mode,
                verdict=verdict,
                final_score=final_score,
                confidence=confidence,
                as_of=as_of,
                available_at=available_at,
                computed_at=computed_at,
                components=components,
                evidence=evidence,
                algorithm_version=algorithm_version,
                summary=summary,
                quality=quality,
            )

            # Validate domain consistency
            metric_keys = {mid: cited_metric_info[mid][2] for mid in cited_metric_ids}
            validate_diagnostic_internal_consistency(diag, metric_keys)
            results[did] = diag

    return results


def fetch_snapshots_chunked(
    connection: DuckDBPyConnection,
    snapshot_ids: Collection[UUID],
) -> dict[UUID, AnalysisSnapshot]:
    """Hydrate AnalysisSnapshot models in chunks of <= 256 without N+1 queries.

    Verifies cited diagnostics, metrics, evidence sets and recalculates evidence_set_digest.
    """
    ordered_ids = tuple(sorted(set(snapshot_ids), key=str))
    if not ordered_ids:
        return {}

    results: dict[UUID, AnalysisSnapshot] = {}
    id_chunks = chunked_sequence(ordered_ids, MAX_CHUNK_SIZE)

    for chunk in id_chunks:
        chunk_str = [str(item) for item in chunk]
        placeholders = ", ".join("?" for _ in chunk)

        # 1. Snapshot rows
        rows = connection.execute(
            f"SELECT snapshot_id, asset_id, domain, known_at, policy_version, "
            f"evidence_set_digest, created_at "
            f"FROM {_SNAP_TABLE} WHERE snapshot_id IN ({placeholders})",
            chunk_str,
        ).fetchall()

        rows_by_id = {str(row[0]): row for row in rows}
        for sid in chunk:
            if str(sid) not in rows_by_id:
                raise RecordNotFoundError(f"analysis snapshot {sid} not found")

        # 2. Metric links
        m_rows = connection.execute(
            f"SELECT snapshot_id, position, metric_result_id "
            f"FROM {_SNAP_METRIC_LINKS} WHERE snapshot_id IN ({placeholders}) "
            "ORDER BY snapshot_id, position",
            chunk_str,
        ).fetchall()
        metric_links: dict[str, list[tuple[int, UUID]]] = collections.defaultdict(list)
        for s_id, pos, mid in m_rows:
            metric_links[str(s_id)].append((int(pos), UUID(str(mid))))

        # 3. Diagnostic links
        d_rows = connection.execute(
            f"SELECT snapshot_id, position, diagnostic_id "
            f"FROM {_SNAP_DIAG_LINKS} WHERE snapshot_id IN ({placeholders}) "
            "ORDER BY snapshot_id, position",
            chunk_str,
        ).fetchall()
        diag_links: dict[str, list[tuple[int, UUID]]] = collections.defaultdict(list)
        for s_id, pos, did in d_rows:
            diag_links[str(s_id)].append((int(pos), UUID(str(did))))

        # Collect cited diagnostics
        all_cited_diag_ids: set[UUID] = set()
        for d_list in diag_links.values():
            for _, did in d_list:
                all_cited_diag_ids.add(did)

        # Hydrate all cited diagnostics using chunked fetch
        diagnostics_by_id = fetch_diagnostics_chunked(connection, all_cited_diag_ids)

        # Collect all metric IDs: direct + diagnostic
        direct_metric_ids_by_snap: dict[str, list[UUID]] = {}
        all_metric_ids: set[UUID] = set()
        for sid in chunk:
            sid_str = str(sid)
            m_list = metric_links.get(sid_str, [])
            validate_link_positions([m[0] for m in m_list], "snapshot metric links")
            m_ids = [m[1] for m in m_list]
            direct_metric_ids_by_snap[sid_str] = m_ids
            all_metric_ids.update(m_ids)

        for diag in diagnostics_by_id.values():
            for comp in diag.components:
                all_metric_ids.update(comp.metric_result_ids)
            for ev in diag.evidence:
                all_metric_ids.add(ev.metric_result_id)

        # Fetch metric metadata (asset, avail, key, evidence_set_id) in chunks <= 256
        metric_info: dict[UUID, tuple[str, datetime, str, str | None]] = {}
        for m_chunk in chunked_sequence(sorted(all_metric_ids, key=str), MAX_CHUNK_SIZE):
            m_placeholders = ", ".join("?" for _ in m_chunk)
            m_rows = connection.execute(
                f"SELECT result_id, asset_id, available_at, metric_key, evidence_set_id "
                f"FROM {_METRIC_TABLE} WHERE result_id IN ({m_placeholders})",
                [str(item) for item in m_chunk],
            ).fetchall()
            for r_id, a_id, avail, key, es_id in m_rows:
                metric_info[UUID(str(r_id))] = (
                    str(a_id),
                    parse_instant_utc(avail, "metric available_at"),
                    str(key),
                    str(es_id) if es_id is not None else None,
                )

        for mid in all_metric_ids:
            if mid not in metric_info:
                raise RecordNotFoundError(f"snapshot references missing metric {mid}")

        # Collect all referenced EvidenceSets
        all_es_ids: set[str] = {info[3] for info in metric_info.values() if info[3] is not None}
        es_info: dict[str, tuple[str, datetime, str]] = {}
        for es_chunk in chunked_sequence(sorted(all_es_ids), MAX_CHUNK_SIZE):
            es_placeholders = ", ".join("?" for _ in es_chunk)
            es_rows = connection.execute(
                f"SELECT evidence_set_id, asset_id, available_at, canonical_hash "
                f"FROM {_ES_TABLE} WHERE evidence_set_id IN ({es_placeholders})",
                list(es_chunk),
            ).fetchall()
            for e_id, a_id, avail, c_hash in es_rows:
                es_info[str(e_id)] = (
                    str(a_id),
                    parse_instant_utc(avail, "evidence set available_at"),
                    str(c_hash),
                )

        for es_id in all_es_ids:
            if es_id not in es_info:
                raise RecordNotFoundError(
                    f"snapshot metric references missing evidence set {es_id}"
                )

        # Assemble and verify snapshots
        for sid in chunk:
            sid_str = str(sid)
            row = rows_by_id[sid_str]
            asset_id = str(row[1])
            domain = str(row[2])
            require_authorized_domain(domain)
            known_at = parse_instant_utc(row[3], "snapshot known_at")
            policy_version = str(row[4])
            evidence_set_digest = str(row[5])
            created_at = parse_instant_utc(row[6], "snapshot created_at")

            direct_mids = tuple(direct_metric_ids_by_snap[sid_str])
            d_list = diag_links.get(sid_str, [])
            validate_link_positions([d[0] for d in d_list], "snapshot diagnostic links")
            diag_ids = tuple(d[1] for d in d_list)

            # Check direct metrics
            snap_es_ids: set[str] = set()
            for mid in direct_mids:
                m_asset, m_avail, m_key, es_id = metric_info[mid]
                if m_asset != asset_id:
                    raise AnalyticalV2ValidationError(
                        f"snapshot for {asset_id} references foreign metric {mid} "
                        f"belonging to {m_asset}"
                    )
                if m_avail > known_at:
                    raise AnalyticalV2ValidationError(
                        f"snapshot cut at {known_at.isoformat()} references future metric "
                        f"{mid} available at {m_avail.isoformat()}"
                    )
                validate_metric_key_for_domain(m_key, domain)
                if es_id:
                    snap_es_ids.add(es_id)

            # Check diagnostics and collect their metrics' evidence sets
            for did in diag_ids:
                diag = diagnostics_by_id[did]
                if diag.asset_id != asset_id:
                    raise AnalyticalV2ValidationError(
                        f"snapshot for {asset_id} references foreign diagnostic {did} "
                        f"belonging to {diag.asset_id}"
                    )
                if diag.available_at > known_at:
                    raise AnalyticalV2ValidationError(
                        f"snapshot cut at {known_at.isoformat()} references future diagnostic "
                        f"{did} available at {diag.available_at.isoformat()}"
                    )
                # Check all cited metrics of diag belong to snapshot domain
                diag_mids: set[UUID] = set()
                for comp in diag.components:
                    diag_mids.update(comp.metric_result_ids)
                for ev in diag.evidence:
                    diag_mids.add(ev.metric_result_id)
                for mid in diag_mids:
                    _, _, m_key, es_id = metric_info[mid]
                    validate_metric_key_for_domain(m_key, domain)
                    if es_id:
                        snap_es_ids.add(es_id)

            # Resolve and verify EvidenceSets for this snapshot
            snap_es_hashes: list[str] = []
            for es_id in sorted(snap_es_ids):
                es_asset, es_avail, es_hash = es_info[es_id]
                if es_asset != asset_id:
                    raise AnalyticalV2ValidationError(
                        f"snapshot for {asset_id} references foreign evidence set {es_id} "
                        f"belonging to {es_asset}"
                    )
                if es_avail > known_at:
                    raise AnalyticalV2ValidationError(
                        f"snapshot cut at {known_at.isoformat()} references future evidence set "
                        f"{es_id} available at {es_avail.isoformat()}"
                    )
                snap_es_hashes.append(es_hash)

            expected_digest = canonical_evidence_set_digest(snap_es_hashes)
            if expected_digest != evidence_set_digest:
                raise AnalyticalV2ValidationError(
                    f"snapshot evidence_set_digest {evidence_set_digest} does not match "
                    f"resolved hashes digest {expected_digest}"
                )

            snapshot = AnalysisSnapshot(
                snapshot_id=sid,
                asset_id=asset_id,
                domain=domain,
                known_at=known_at,
                policy_version=policy_version,
                metric_ids=direct_mids,
                diagnostic_ids=diag_ids,
                evidence_set_digest=evidence_set_digest,
                created_at=created_at,
            )
            results[sid] = snapshot

    return results
