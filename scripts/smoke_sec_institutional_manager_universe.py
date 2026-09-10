#!/usr/bin/env python3
"""Run real SEC Form 13F manager universe acquisition and rerun in a temporary workspace.

Validates the official SEC catalog discovery, archive retrieval, streaming parsing, deterministic
selection, idempotence, and point-in-time query against official sec.gov endpoints.
The configured SEC_USER_AGENT value is never printed or persisted.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseQueryRequest,
    SecInstitutionalUniverseRefreshRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport


class _CountingSecTransport:
    """Count SEC requests by host and path surface while delegating to the real transport."""

    def __init__(self) -> None:
        self._transport = UrlLibHttpTransport()
        self.catalog_calls = 0
        self.zip_calls = 0
        self.other_sec_calls = 0
        self.third_party_calls = 0

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        if parsed.hostname == "www.sec.gov":
            if parsed.path.startswith("/data-research/sec-markets-data/form-13f-data-sets"):
                self.catalog_calls += 1
            elif parsed.path.startswith("/files/structureddata/data/form-13f-data-sets/"):
                self.zip_calls += 1
            else:
                self.other_sec_calls += 1
        elif parsed.hostname and "sec.gov" in parsed.hostname:
            self.other_sec_calls += 1
        else:
            self.third_party_calls += 1

        return self._transport.get(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )


def _get_peak_rss_kib() -> int:
    """Return maximum resident set size in KiB across platforms."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On Linux ru_maxrss is in KiB; on macOS it is in bytes
    if sys.platform == "darwin":
        return usage // 1024
    return usage


def main(argv: list[str] | None = None) -> int:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        sys.stderr.write("Error: SEC_USER_AGENT environment variable is required\n")
        return 1

    sec_identity = SecEdgarIdentity(user_agent)
    transport = _CountingSecTransport()
    runtime = ApplicationRuntime.create_default()
    application = SecInstitutionalUniverseApplication(
        runtime,
        transport_factory=lambda: transport,
    )

    with TemporaryDirectory(prefix="investment-analyst-sec-13f-smoke-") as temporary:
        location = StorageLocationRequest(legacy_root=Path(temporary))

        start_time = time.monotonic()
        # First execution: download, parse, persist
        first_result = application.refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=sec_identity,
            location=location,
        )
        duration_first = time.monotonic() - start_time

        # Assertions on first run
        if not first_result.created:
            raise RuntimeError("First run expected created=True for new dataset blob")
        if first_result.candidate_manager_count == 0:
            raise RuntimeError("Expected > 0 candidates for official 13F dataset")
        if first_result.selected_manager_count > 25:
            raise RuntimeError(
                f"Expected <= 25 selected managers, got {first_result.selected_manager_count}"
            )
        if first_result.matched_asset_count == 0:
            raise RuntimeError("Expected at least one matched catalog asset")

        first_catalog_calls = transport.catalog_calls
        first_zip_calls = transport.zip_calls
        if first_catalog_calls != 1 or first_zip_calls != 1:
            raise RuntimeError(
                f"Expected 1 catalog and 1 ZIP GET, got {first_catalog_calls}, {first_zip_calls}"
            )
        if transport.other_sec_calls != 0 or transport.third_party_calls != 0:
            raise RuntimeError(
                "Zero Submissions, Archives, Company Facts or third-party calls expected"
            )

        # Second execution: rerun for idempotence
        start_rerun = time.monotonic()
        second_result = application.refresh(
            SecInstitutionalUniverseRefreshRequest(),
            sec_identity=sec_identity,
            location=location,
        )
        duration_second = time.monotonic() - start_rerun

        # Assertions on rerun (idempotence)
        if second_result.created:
            raise RuntimeError("Rerun expected created=False (reusing existing blob)")
        if second_result.revision_id != first_result.revision_id:
            raise RuntimeError("Rerun revision_id mismatch")
        if second_result.snapshot_id != first_result.snapshot_id:
            raise RuntimeError("Rerun snapshot_id mismatch")
        if second_result.candidate_manager_count != first_result.candidate_manager_count:
            raise RuntimeError("Rerun candidate count mismatch")

        # Third: read-only query
        query_result = application.query(
            SecInstitutionalUniverseQueryRequest(
                known_at=first_result.retrieved_at,
                asset_id="equity:us:aapl",
            ),
            location=location,
        )
        if query_result.snapshot.snapshot_id != first_result.snapshot_id:
            raise RuntimeError("Query snapshot_id mismatch")

        peak_rss = _get_peak_rss_kib()

        smoke_summary = {
            "status": "PASS",
            "block": "SEC-CORPUS-27",
            "url": first_result.dataset_url,
            "period_start": first_result.period_start.isoformat(),
            "period_end": first_result.period_end.isoformat(),
            "dataset_sha256": first_result.dataset_sha256,
            "size_bytes": first_result.size_bytes,
            "candidate_manager_count": first_result.candidate_manager_count,
            "selected_manager_count": first_result.selected_manager_count,
            "unselected_manager_count": first_result.unselected_manager_count,
            "matched_asset_count": first_result.matched_asset_count,
            "eligible_asset_count": first_result.eligible_asset_count,
            "coverage_complete": first_result.coverage_complete,
            "transport_calls": {
                "catalog_calls": transport.catalog_calls,
                "zip_calls": transport.zip_calls,
                "other_sec_calls": transport.other_sec_calls,
                "third_party_calls": transport.third_party_calls,
            },
            "durations_seconds": {
                "first_run": round(duration_first, 3),
                "rerun": round(duration_second, 3),
            },
            "peak_rss_kib": peak_rss,
            "idempotence_verified": True,
            "query_verified": True,
        }

        sys.stdout.write(json.dumps(smoke_summary, indent=2) + "\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
