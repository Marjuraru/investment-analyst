#!/usr/bin/env python3
"""Smoke real de la adquisición dirigida y reanudable de filings SEC Form 13F.

Materializa el último universo oficial de gestores en un workspace temporal, procesa una página de
un gestor y drena el backlog de accessions hasta cero antes de medir la repetición terminal. La
identidad configurada en `SEC_USER_AGENT` nunca se imprime ni se persiste, y el workspace
permanente queda fuera de toda escritura.
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
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport
from investment_analyst.storage import LocalStorage, StoragePaths

ACCESSIONS_PER_MANAGER = 5
MAX_DRAIN_ITERATIONS = 4
_CALL_KINDS = ("catalog", "zip", "submissions", "archives", "other_sec", "third_party")


class _CountingSecTransport:
    """Count SEC requests by host and path surface while delegating to the real transport."""

    def __init__(self) -> None:
        self._transport = UrlLibHttpTransport()
        self.counts = dict.fromkeys(_CALL_KINDS, 0)

    def snapshot(self) -> dict[str, int]:
        return dict(self.counts)

    def delta(self, baseline: dict[str, int]) -> dict[str, int]:
        return {kind: self.counts[kind] - baseline[kind] for kind in _CALL_KINDS}

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
    """Read the persisted 13F evidence counts without writing anything."""
    with LocalStorage(StoragePaths.from_root(workspace_root), read_only=True) as storage:
        return {
            "reports": storage.raw_records.count(
                schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION
            ),
            "semantics": storage.raw_records.count(
                schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION
            ),
            "total_raw_records": storage.raw_records.count(),
        }


def main() -> int:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        sys.stderr.write("Error: SEC_USER_AGENT environment variable is required\n")
        return 1

    identity = SecEdgarIdentity(user_agent)
    transport = _CountingSecTransport()
    runtime = ApplicationRuntime.create_default()

    with TemporaryDirectory(prefix="investment-analyst-sec-13f-directed-") as temporary:
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
        universe_calls = transport.snapshot()

        request = SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=datetime.now(UTC),
            manager_limit=1,
            accessions_per_manager=ACCESSIONS_PER_MANAGER,
        )
        application = SecInstitutionalHoldingsDirectedRefreshApplication(
            runtime, transport_factory=lambda: transport
        )

        first_started = time.monotonic()
        baseline = transport.snapshot()
        first = application.refresh(request, sec_identity=identity, location=location)
        first_seconds = time.monotonic() - first_started
        first_calls = transport.delta(baseline)

        if first.page_manager_count != 1:
            raise RuntimeError(f"expected exactly one manager page, got {first.page_manager_count}")
        if first_calls["submissions"] != 1:
            raise RuntimeError(f"expected one Submissions GET, got {first_calls['submissions']}")
        if first_calls["third_party"] or first_calls["other_sec"]:
            raise RuntimeError("unexpected non-SEC or out-of-contract SEC calls")
        if first.backlog_after > 0 and first.archives_calls == 0:
            raise RuntimeError("a non-empty backlog must attempt SEC Archives")

        drain_runs = []
        terminal = first
        terminal_calls = first_calls
        terminal_seconds = first_seconds
        while terminal.backlog_after > 0 and len(drain_runs) < MAX_DRAIN_ITERATIONS:
            drain_baseline = transport.snapshot()
            started = time.monotonic()
            terminal = application.refresh(request, sec_identity=identity, location=location)
            duration = time.monotonic() - started
            terminal_calls = transport.delta(drain_baseline)
            drain_runs.append(
                {
                    "backlog_after": terminal.backlog_after,
                    "created": terminal.created,
                    "reused": terminal.reused,
                    "archives_calls": terminal_calls["archives"],
                    "duration_seconds": round(duration, 3),
                }
            )
            terminal_seconds = duration
        if terminal.backlog_after != 0:
            raise RuntimeError("bounded drain did not reach an empty backlog")

        before_repeat = _cardinality(workspace_root)
        repeat_baseline = transport.snapshot()
        repeat_started = time.monotonic()
        repeat = application.refresh(request, sec_identity=identity, location=location)
        repeat_seconds = time.monotonic() - repeat_started
        repeat_calls = transport.delta(repeat_baseline)
        after_repeat = _cardinality(workspace_root)

        if repeat_calls["submissions"] != 1:
            raise RuntimeError(f"terminal repeat must do one Submissions GET, got {repeat_calls}")
        if repeat_calls["archives"] != 0 or repeat.archives_calls != 0:
            raise RuntimeError("terminal repeat must not fetch SEC Archives")
        if repeat.created != 0 or repeat.semantics_created != 0:
            raise RuntimeError("terminal repeat must create no report or semantic artifact")
        repeated_page = repeat.managers[0]
        if repeated_page.reused_accessions != repeated_page.eligible_accessions:
            raise RuntimeError("terminal repeat must reuse every eligible accession")
        if repeated_page.backlog_after != 0:
            raise RuntimeError("terminal repeat must leave an empty backlog")
        if before_repeat != after_repeat:
            raise RuntimeError("terminal repeat must not change persisted cardinality")
        if repeat.traceability_verified is not True:
            raise RuntimeError("terminal repeat must verify traceability")

        summary = {
            "status": "PASS",
            "block": "SEC-CORPUS-28",
            "universe": {
                "snapshot_id": str(universe.snapshot_id),
                "dataset_revision_id": str(universe.revision_id),
                "dataset_sha256": universe.dataset_sha256,
                "period_start": universe.period_start.isoformat(),
                "period_end": universe.period_end.isoformat(),
                "selected_manager_count": universe.selected_manager_count,
                "coverage_complete": universe.coverage_complete,
                "calls": universe_calls,
                "duration_seconds": round(universe_seconds, 3),
            },
            "page": {
                "manager_cik": first.managers[0].manager_cik,
                "report_period": first.managers[0].report_period.isoformat(),
                "eligible_accessions": list(first.managers[0].eligible_accessions),
                "created_accessions": list(first.managers[0].created_accessions),
                "reused_accessions": list(first.managers[0].reused_accessions),
                "rejected_accessions": list(first.managers[0].rejected_accessions),
                "failed_accessions": list(first.managers[0].failed_accessions),
                "pending_before": first.managers[0].pending_before,
                "calls": first_calls,
                "duration_seconds": round(first_seconds, 3),
            },
            "drain_runs": drain_runs,
            "terminal": {
                "backlog_after": terminal.backlog_after,
                "created": terminal.created,
                "reused": terminal.reused,
                "semantics_created": terminal.semantics_created,
                "semantics_reused": terminal.semantics_reused,
                "archives_calls": terminal.archives_calls,
                "duration_seconds": round(terminal_seconds, 3),
            },
            "repeat": {
                "created": repeat.created,
                "reused": repeat.reused,
                "semantics_created": repeat.semantics_created,
                "semantics_reused": repeat.semantics_reused,
                "calls": repeat_calls,
                "duration_seconds": round(repeat_seconds, 3),
                "cardinality_before": before_repeat,
                "cardinality_after": after_repeat,
            },
            "idempotence_verified": True,
            "traceability_verified": True,
        }
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
