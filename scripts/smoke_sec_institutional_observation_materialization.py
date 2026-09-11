#!/usr/bin/env python3
"""Smoke real de la materialización dirigida de observaciones institucionales 13F.

En un workspace temporal exclusivamente: refresca el universo oficial de gestores, adquiere una
página de un gestor con `SEC-CORPUS-28`, materializa correspondencias y observaciones con
`SEC-CORPUS-29` y consulta el camino de lectura local ya integrado para AAPL. La repetición de #29
debe hacer cero llamadas de red, crear cero registros y conservar identidades y cardinalidad. La
identidad configurada en `SEC_USER_AGENT` nunca se imprime ni se persiste, y el workspace permanente
queda fuera de toda escritura.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from investment_analyst.application.cazatiburones_institutional_observations import (
    CazatiburonesInstitutionalObservationsApplication,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_holdings_refresh import (
    SecInstitutionalHoldingsDirectedRefreshApplication,
)
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    SecInstitutionalHoldingsDirectedRefreshRequest,
)
from investment_analyst.application.sec_institutional_observation_materialization import (
    SecInstitutionalObservationMaterializationApplication,
)
from investment_analyst.application.sec_institutional_observation_materialization_models import (
    SecInstitutionalObservationMaterializationRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import (
    SOURCE_ID as OBSERVATION_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    observation_lineage_key,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport
from investment_analyst.storage import LocalStorage, StoragePaths

ASSET_ID = "equity:us:aapl"
ACCESSIONS_PER_MANAGER = 5
MAX_DRAIN_ITERATIONS = 4
_CALL_KINDS = ("catalog", "zip", "submissions", "archives", "other_sec", "third_party")


class _CountingSecTransport:
    """Count SEC requests by host and path surface while delegating to the real transport."""

    def __init__(self) -> None:
        self._transport = UrlLibHttpTransport()
        self.counts = dict.fromkeys(_CALL_KINDS, 0)

    def total(self) -> int:
        return sum(self.counts.values())

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        path = parsed.path
        if host == "data.sec.gov" and path.startswith("/submissions/"):
            kind = "submissions"
        elif host == "www.sec.gov" and path.startswith("/Archives/"):
            kind = "archives"
        elif host == "www.sec.gov" and path.startswith(
            "/data-research/sec-markets-data/form-13f-data-sets"
        ):
            kind = "catalog"
        elif host == "www.sec.gov" and path.startswith(
            "/files/structureddata/data/form-13f-data-sets/"
        ):
            kind = "zip"
        elif host.endswith("sec.gov"):
            kind = "other_sec"
        else:
            kind = "third_party"
        self.counts[kind] += 1
        return self._transport.get(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )


def _cardinality(workspace_root: Path) -> dict[str, int]:
    """Read the persisted evidence counts without writing anything."""
    with LocalStorage(StoragePaths.from_root(workspace_root), read_only=True) as storage:
        return {
            "row_correspondences": storage.raw_records.count(
                schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION
            ),
            "total_raw_records": storage.raw_records.count(),
        }


def _observed_claims(workspace_root: Path, known_at: datetime) -> int:
    with LocalStorage(StoragePaths.from_root(workspace_root), read_only=True) as storage:
        return len(
            [
                observation
                for observation in storage.observations.list(
                    asset_id=ASSET_ID, source_id=OBSERVATION_SOURCE_ID, available_to=known_at
                )
                if observation_lineage_key(observation)["correspondence_id"]
            ]
        )


def main() -> int:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        sys.stderr.write("Error: SEC_USER_AGENT environment variable is required\n")
        return 1

    identity = SecEdgarIdentity(user_agent)
    transport = _CountingSecTransport()
    runtime = ApplicationRuntime.create_default()

    with TemporaryDirectory(prefix="investment-analyst-sec-13f-observation-") as temporary:
        workspace_root = Path(temporary)
        location = StorageLocationRequest(legacy_root=workspace_root)

        universe_started = time.monotonic()
        universe = SecInstitutionalUniverseApplication(
            runtime, transport_factory=lambda: transport
        ).refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=identity,
            location=location,
        )
        universe_seconds = time.monotonic() - universe_started
        universe_calls = transport.total()

        directed = SecInstitutionalHoldingsDirectedRefreshApplication(
            runtime, transport_factory=lambda: transport
        )
        request = SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=datetime.now(UTC),
            manager_limit=1,
            accessions_per_manager=ACCESSIONS_PER_MANAGER,
        )
        observations_request = SecInstitutionalObservationMaterializationRequest(
            known_at=datetime.now(UTC), manager_limit=1
        )

        acquisition_started = time.monotonic()
        acquisition = directed.refresh(request, sec_identity=identity, location=location)
        drain_runs = 0
        while acquisition.backlog_after > 0 and drain_runs < MAX_DRAIN_ITERATIONS:
            acquisition = directed.refresh(request, sec_identity=identity, location=location)
            drain_runs += 1
        acquisition_seconds = time.monotonic() - acquisition_started
        if acquisition.backlog_after != 0:
            raise RuntimeError("bounded acquisition drain did not reach an empty backlog")
        if not acquisition.managers or not acquisition.managers[0].report_ids:
            raise RuntimeError("acquisition produced no report for the selected manager page")

        application = SecInstitutionalObservationMaterializationApplication(runtime)
        before_materialization = transport.total()
        materialization_started = time.monotonic()
        first = application.materialize(observations_request, location=location)
        materialization_seconds = time.monotonic() - materialization_started
        network_calls = transport.total() - before_materialization

        if network_calls != 0:
            raise RuntimeError(
                f"materialization must not perform network calls, got {network_calls}"
            )
        if first.claims_created + first.claims_reused < 1:
            raise RuntimeError("materialization produced no row-scoped correspondence claim")
        if first.observations_created + first.observations_reused < 1:
            raise RuntimeError("materialization produced no institutional observation")
        if first.traceability_verified is not True:
            raise RuntimeError("materialization traceability did not verify")

        observed_before = _observed_claims(workspace_root, observations_request.known_at)
        before_repeat = _cardinality(workspace_root)

        repeat_started = time.monotonic()
        repeat = application.materialize(observations_request, location=location)
        repeat_seconds = time.monotonic() - repeat_started
        after_repeat = _cardinality(workspace_root)
        observed_after = _observed_claims(workspace_root, observations_request.known_at)

        if repeat.claims_created != 0 or repeat.observations_created != 0:
            raise RuntimeError("repeat materialization must create no claim or observation")
        if repeat.claims_reused != first.claims_created + first.claims_reused:
            raise RuntimeError("repeat materialization must reuse every proven claim")
        if before_repeat != after_repeat:
            raise RuntimeError("repeat materialization must not change persisted cardinality")
        if observed_before != observed_after:
            raise RuntimeError("repeat materialization must not change observable observations")
        if repeat.traceability_verified is not True:
            raise RuntimeError("repeat materialization traceability did not verify")

        query = CazatiburonesInstitutionalObservationsApplication(runtime).query(
            InstitutionalObservationQuery(
                asset_id=ASSET_ID, known_at=observations_request.known_at
            ),
            location=location,
        )
        if query.total_matching < 1 or not query.observations:
            raise RuntimeError("the integrated local read path returned no AAPL observation")
        first_view = query.observations[0]
        if first_view.observation.asset_id != ASSET_ID:
            raise RuntimeError("the local read path returned a different asset")

        manager = acquisition.managers[0]
        summary = {
            "status": "PASS",
            "block": "SEC-CORPUS-29",
            "universe": {
                "snapshot_id": str(universe.snapshot_id),
                "dataset_revision_id": str(universe.revision_id),
                "dataset_sha256": universe.dataset_sha256,
                "period_start": universe.period_start.isoformat(),
                "period_end": universe.period_end.isoformat(),
                "selected_manager_count": universe.selected_manager_count,
                "coverage_complete": universe.coverage_complete,
                "network_calls": universe_calls,
                "duration_seconds": round(universe_seconds, 3),
            },
            "acquisition": {
                "manager_cik": manager.manager_cik,
                "report_period": manager.report_period.isoformat(),
                "created_accessions": list(manager.created_accessions),
                "reused_accessions": list(manager.reused_accessions),
                "report_ids": [str(value) for value in manager.report_ids],
                "backlog_after": acquisition.backlog_after,
                "drain_runs": drain_runs,
                "duration_seconds": round(acquisition_seconds, 3),
            },
            "materialization": {
                "snapshot_id": str(first.snapshot_id),
                "dataset_revision_id": str(first.dataset_revision_id),
                "page_manager_count": first.page_manager_count,
                "candidate_count": first.candidate_count,
                "claims_created": first.claims_created,
                "claims_reused": first.claims_reused,
                "claims_ambiguous": first.claims_ambiguous,
                "observations_created": first.observations_created,
                "observations_reused": first.observations_reused,
                "failed_candidates": first.failed_candidates,
                "failed_runs": first.failed_runs,
                "claim_ids": [str(value) for value in first.candidates[0].claim_ids],
                "states": sorted({item.state for item in first.candidates}),
                "skipped_by_reason": first.candidates[0].skipped_by_reason,
                "network_calls": network_calls,
                "duration_seconds": round(materialization_seconds, 3),
            },
            "repeat": {
                "claims_created": repeat.claims_created,
                "claims_reused": repeat.claims_reused,
                "observations_created": repeat.observations_created,
                "observations_reused": repeat.observations_reused,
                "network_calls": transport.total() - before_materialization - network_calls,
                "duration_seconds": round(repeat_seconds, 3),
                "cardinality_before": before_repeat,
                "cardinality_after": after_repeat,
            },
            "read_path": {
                "asset_id": ASSET_ID,
                "total_matching": query.total_matching,
                "returned": len(query.observations),
                "field_name": first_view.observation.field_name,
                "unit": first_view.observation.unit,
                "cusip": first_view.row.cusip,
                "correspondence_kind": type(first_view.correspondence).__name__,
                "manager_cik": first_view.artifact.manager_cik,
                "observation_ids": [
                    str(item.observation.observation_id) for item in query.observations
                ],
            },
            "observed_claims": observed_before,
            "network_calls": dict(transport.counts),
            "idempotence_verified": True,
            "traceability_verified": True,
        }
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
