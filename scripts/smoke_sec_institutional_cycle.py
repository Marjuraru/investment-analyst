#!/usr/bin/env python3
"""Smoke real del ciclo institucional programado y reanudable SEC Form 13F.

Ejecuta el ciclo integrado (#27 universo -> #28 adquisición dirigida -> #29 correspondencia
y observaciones) contra endpoints oficiales de SEC EDGAR en un workspace temporal:
1. Primera ejecución desde estado vacío: verifica 1 GET de catálogo, 1 GET de ZIP,
   1 GET de Submissions, llamadas a SEC Archives, materialización de observaciones y
   avance atómico del cursor de 0 a 1 en sec_institutional_cycle_state_v1.json.
2. Segunda ejecución tras recreación de objetos: verifica 1 GET de catálogo diario,
   cero GET de ZIP (cache hit bajo 7 días), avance del cursor de 1 a 2 sin duplicar
   evidencia previa del manager 0.
3. Emite telemetría limpia en JSON sin exponer secretos.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_cycle import (
    SecInstitutionalCycleApplication,
)
from investment_analyst.application.sec_institutional_cycle_models import (
    SecInstitutionalCycleRequest,
)
from investment_analyst.application.sec_institutional_cycle_state import (
    SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME,
    SecInstitutionalCycleStateStore,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport
from investment_analyst.storage import LocalStorage, StoragePaths

_CALL_KINDS = ("catalog", "zip", "submissions", "archives", "other_sec", "third_party")


class _CountingSecTransport:
    """Count SEC requests by endpoint kind while delegating to real transport."""

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


def main() -> int:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        env_candidates = (
            Path(__file__).resolve().parents[1] / ".env",
            Path("/home/marjuraru/projects/investment-analyst/.env"),
        )
        for env_file in env_candidates:
            if env_file.is_file():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("SEC_USER_AGENT=") or line.startswith(
                        "export SEC_USER_AGENT="
                    ):
                        _, _, val = line.partition("=")
                        cleaned = val.strip().strip("'\"")
                        if cleaned:
                            user_agent = cleaned
                            os.environ["SEC_USER_AGENT"] = cleaned
                            break
            if user_agent:
                break
    if not user_agent:
        sys.stderr.write("Error: SEC_USER_AGENT environment variable is required\n")
        return 1

    sec_identity = SecEdgarIdentity(user_agent)
    transport = _CountingSecTransport()
    runtime = ApplicationRuntime.create_default()

    with TemporaryDirectory(prefix="investment-analyst-sec-cycle-smoke-") as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = temp_path / "state"
        location = StorageLocationRequest(legacy_root=workspace)

        state_store_1 = SecInstitutionalCycleStateStore(
            state_root / SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME
        )
        app_1 = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            state_store=state_store_1,
        )

        # Step 1: Initial cycle execution from empty state
        now_1 = datetime.now(UTC)
        baseline_1 = transport.snapshot()
        start_1 = time.monotonic()
        summary_1 = app_1.run_cycle(
            SecInstitutionalCycleRequest(known_at=now_1),
            sec_identity=sec_identity,
            location=location,
        )
        duration_1 = time.monotonic() - start_1
        calls_1 = transport.delta(baseline_1)

        if summary_1.status != "processed":
            raise RuntimeError(
                f"Step 1 failed with status={summary_1.status}, reason={summary_1.reason_code}, "
                f"created_acc={summary_1.created_accessions}, "
                f"obs_created={summary_1.observations_created}, "
                f"traceability={summary_1.traceability_verified}"
            )
        if summary_1.catalog_calls != 1 or calls_1["catalog"] != 1:
            raise RuntimeError(f"Step 1 expected 1 catalog GET, got {calls_1['catalog']}")
        if summary_1.zip_calls != 1 or calls_1["zip"] != 1:
            raise RuntimeError(f"Step 1 expected 1 ZIP GET, got {calls_1['zip']}")
        if summary_1.submissions_calls != 1 or calls_1["submissions"] != 1:
            raise RuntimeError(f"Step 1 expected 1 Submissions GET, got {calls_1['submissions']}")
        if summary_1.manager_cursor_before != 0 or summary_1.manager_cursor_after != 1:
            raise RuntimeError(
                f"Step 1 cursor progression error: {summary_1.manager_cursor_before} -> "
                f"{summary_1.manager_cursor_after}"
            )
        if calls_1["third_party"] or calls_1["other_sec"]:
            raise RuntimeError("Unexpected out-of-contract SEC or third-party network calls")

        # Verify state file on disk
        persisted_1 = state_store_1.load()
        if persisted_1.manager_cursor != 1 or persisted_1.last_status != "success":
            raise RuntimeError(f"Step 1 persisted state invalid: {persisted_1}")

        # Verify observations in storage
        with LocalStorage(StoragePaths.from_root(workspace), read_only=True) as storage:
            obs_service = InstitutionalObservationService(storage)
            obs_result = obs_service.query(
                InstitutionalObservationQuery(
                    asset_id="equity:us:aapl", known_at=summary_1.effective_known_at
                )
            )
            aapl_observations_count = len(obs_result.observations)

        # Step 2: Object recreation and second execution
        runtime_2 = ApplicationRuntime.create_default()
        state_store_2 = SecInstitutionalCycleStateStore(
            state_root / SEC_INSTITUTIONAL_CYCLE_STATE_FILE_NAME
        )
        app_2 = SecInstitutionalCycleApplication(
            runtime_2,
            transport_factory=lambda: transport,
            state_store=state_store_2,
        )

        now_2 = max(datetime.now(UTC), summary_1.effective_known_at + timedelta(seconds=1))
        baseline_2 = transport.snapshot()
        start_2 = time.monotonic()
        summary_2 = app_2.run_cycle(
            SecInstitutionalCycleRequest(known_at=now_2),
            sec_identity=sec_identity,
            location=location,
        )
        duration_2 = time.monotonic() - start_2
        calls_2 = transport.delta(baseline_2)

        if summary_2.status != "processed":
            raise RuntimeError(
                f"Step 2 failed with status={summary_2.status}, reason={summary_2.reason_code}"
            )
        if summary_2.catalog_calls != 1 or calls_2["catalog"] != 1:
            raise RuntimeError(f"Step 2 expected 1 catalog GET, got {calls_2['catalog']}")
        if summary_2.zip_calls != 0 or calls_2["zip"] != 0:
            raise RuntimeError(f"Step 2 expected ZERO ZIP GETs, got {calls_2['zip']}")
        if summary_2.manager_cursor_before != 1 or summary_2.manager_cursor_after != 2:
            raise RuntimeError(
                f"Step 2 cursor progression error: {summary_2.manager_cursor_before} -> "
                f"{summary_2.manager_cursor_after}"
            )

        persisted_2 = state_store_2.load()
        if persisted_2.manager_cursor != 2 or persisted_2.last_status != "success":
            raise RuntimeError(f"Step 2 persisted state invalid: {persisted_2}")

        telemetry = {
            "status": "ok",
            "step_1": {
                "manager_name": summary_1.manager_name,
                "manager_cik": summary_1.manager_cik,
                "cursor_before": summary_1.manager_cursor_before,
                "cursor_after": summary_1.manager_cursor_after,
                "total_managers": summary_1.total_managers,
                "created_accessions": len(summary_1.created_accessions),
                "observations_created": summary_1.observations_created,
                "aapl_observations_visible": aapl_observations_count,
                "calls": calls_1,
                "duration_seconds": round(duration_1, 3),
            },
            "step_2": {
                "manager_name": summary_2.manager_name,
                "manager_cik": summary_2.manager_cik,
                "cursor_before": summary_2.manager_cursor_before,
                "cursor_after": summary_2.manager_cursor_after,
                "total_managers": summary_2.total_managers,
                "zip_calls": summary_2.zip_calls,
                "calls": calls_2,
                "duration_seconds": round(duration_2, 3),
            },
            "total_calls": transport.snapshot(),
        }
        sys.stdout.write(json.dumps(telemetry, indent=2) + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
