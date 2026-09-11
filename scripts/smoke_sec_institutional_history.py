#!/usr/bin/env python3
"""Smoke real de la ventana histórica de dos cierres 13F (SEC-CORPUS-31).

Ejecuta la ventana integrada de dos datasets oficiales adyacentes contra endpoints reales de
SEC EDGAR en un workspace, state root y outbox temporales:

1. Preparación: como máximo un GET de ZIP por intento hasta completar el par adyacente; la
   fase de preparación no consume ningún gestor.
2. Procesamiento: recrea los objetos entre pasos, resuelve los gestores comunes a ambos cierres
   y procesa un gestor por intento hasta producir una comparación trazable con métricas,
   pesos, eventos institucionales y candidatos elegibles en la outbox.
3. Repetición: no redescarga ZIP, no llama Submissions y no duplica evidencia, eventos ni
   notificaciones.

Emite telemetría limpia en JSON sin exponer secretos, contenido documental ni rutas absolutas.
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

from investment_analyst.alerts.cazatiburones_notifications import CazatiburonesNotificationStore
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_history import (
    SecInstitutionalHistoryApplication,
)
from investment_analyst.application.sec_institutional_history_models import (
    SecInstitutionalHistoryRequest,
)
from investment_analyst.application.sec_institutional_history_state import (
    SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME,
    SecInstitutionalHistoryStateStore,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, UrlLibHttpTransport
from investment_analyst.storage import LocalStorage, StoragePaths

_CALL_KINDS = ("catalog", "zip", "submissions", "archives", "other_sec", "third_party")
_MAX_TARGET_STEPS = 12


class _CountingSecTransport:
    """Count SEC requests by endpoint kind while delegating to the real transport."""

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


def _resolve_user_agent() -> str:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if user_agent:
        return user_agent
    for env_file in (
        Path(__file__).resolve().parents[1] / ".env",
        Path("/home/marjuraru/projects/investment-analyst/.env"),
    ):
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if candidate.startswith("SEC_USER_AGENT=") or candidate.startswith(
                "export SEC_USER_AGENT="
            ):
                _, _, value = candidate.partition("=")
                cleaned = value.strip().strip("'\"")
                if cleaned:
                    os.environ["SEC_USER_AGENT"] = cleaned
                    return cleaned
    return ""


def _period_payload(period) -> dict[str, object]:
    return {
        "role": period.role,
        "period_start": period.period_start.isoformat(),
        "period_end": period.period_end.isoformat(),
        "zip_downloaded": period.zip_downloaded,
        "snapshot_reused": period.snapshot_reused,
    }


def main(telemetry: dict[str, object] | None = None) -> int:
    user_agent = _resolve_user_agent()
    if not user_agent:
        sys.stderr.write("Error: SEC_USER_AGENT environment variable is required\n")
        return 1

    sec_identity = SecEdgarIdentity(user_agent)
    transport = _CountingSecTransport()
    clock_state = [datetime.now(UTC)]

    if telemetry is None:
        telemetry = {}
    telemetry["status"] = "running"
    telemetry["preparation"] = []
    telemetry["targets"] = []

    with TemporaryDirectory(prefix="investment-analyst-13f-history-smoke-") as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = workspace / "state"
        outbox_state = state_root / "cazatiburones_notification_outbox_state_v1.json"
        location = StorageLocationRequest(legacy_root=workspace)

        preparation_steps = 0
        while True:
            preparation_steps += 1
            if preparation_steps > 4:
                raise RuntimeError("the two official datasets were not prepared within four steps")
            clock_state[0] = clock_state[0] + timedelta(minutes=1)
            runtime = ApplicationRuntime.create_default()
            state_store = SecInstitutionalHistoryStateStore(
                state_root / SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME
            )
            application = SecInstitutionalHistoryApplication(
                runtime,
                transport_factory=lambda: transport,
                clock=lambda: clock_state[0],
            )
            baseline = transport.snapshot()
            started = time.monotonic()
            summary = application.run_cycle(
                SecInstitutionalHistoryRequest(known_at=clock_state[0]),
                sec_identity=sec_identity,
                location=location,
                outbox_state=outbox_state,
            )
            calls = transport.delta(baseline)
            telemetry["preparation"].append(
                {
                    "status": summary.status,
                    "phase": summary.phase,
                    "catalog_calls": summary.catalog_calls,
                    "zip_calls": summary.zip_calls,
                    "periods": [_period_payload(item) for item in summary.periods],
                    "calls": calls,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            if calls["third_party"]:
                raise RuntimeError("unexpected third-party network call during preparation")
            if summary.status != "preparing":
                if summary.status == "failed":
                    raise RuntimeError(f"preparation failed: {summary.reason_code}")
                break
            if summary.zip_calls > 1:
                raise RuntimeError("preparation downloaded more than one ZIP in one attempt")
            if summary.submissions_calls:
                raise RuntimeError("preparation consumed a manager before the window was ready")

        if summary.periods:
            telemetry["window"] = [_period_payload(item) for item in summary.periods]
        telemetry["common_manager_count"] = summary.common_manager_count
        telemetry["excluded_manager_count"] = summary.excluded_manager_count
        telemetry["non_comparable_manager_count"] = summary.non_comparable_manager_count
        telemetry["total_targets"] = summary.total_targets

        if summary.total_targets == 0:
            raise RuntimeError(
                "the official window exposes zero common managers with catalog CUSIPs"
            )

        observed_comparison = False
        target_steps = 0
        while summary.status == "processed" and not observed_comparison:
            target_steps += 1
            if target_steps > _MAX_TARGET_STEPS:
                break
            clock_state[0] = clock_state[0] + timedelta(minutes=1)
            runtime = ApplicationRuntime.create_default()
            application = SecInstitutionalHistoryApplication(
                runtime,
                transport_factory=lambda: transport,
                clock=lambda: clock_state[0],
            )
            baseline = transport.snapshot()
            started = time.monotonic()
            summary = application.run_cycle(
                SecInstitutionalHistoryRequest(known_at=clock_state[0]),
                sec_identity=sec_identity,
                location=location,
                outbox_state=outbox_state,
            )
            calls = transport.delta(baseline)
            target = summary.target
            step: dict[str, object] = {
                "status": summary.status,
                "phase": summary.phase,
                "reason_code": summary.reason_code,
                "manager_cik": summary.manager_cik,
                "cursor_before": summary.target_cursor_before,
                "cursor_after": summary.target_cursor_after,
                "calls": calls,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
            if target is not None:
                step["target"] = {
                    "state": target.state,
                    "created_accessions": len(target.created_accessions),
                    "reused_accessions": len(target.reused_accessions),
                    "rejected_accessions": len(target.rejected_accessions),
                    "failed_accessions": len(target.failed_accessions),
                    "observations_created": (
                        target.older_observations_created + target.newer_observations_created
                    ),
                    "metrics_created": target.metrics_created,
                    "weights_created": target.weights_created,
                    "events_created": target.events_created,
                    "non_evaluable": target.non_evaluable,
                }
                observed_comparison = target.metrics_created + target.metrics_reused > 0
            step["notifications"] = {
                "created": summary.notifications_created,
                "reused": summary.notifications_reused,
            }
            telemetry["targets"].append(step)
            if calls["third_party"]:
                raise RuntimeError("unexpected third-party network call during processing")
            if summary.status == "failed":
                raise RuntimeError(f"history target failed: {summary.reason_code}")

        notifications = CazatiburonesNotificationStore(outbox_state).load()
        telemetry["outbox_items"] = len(notifications.items)
        if observed_comparison and not notifications.items:
            raise RuntimeError("a comparable pair produced no institutional notification candidate")

        persisted = state_store.load()
        if persisted.phase != "completed" and summary.status != "processed":
            raise RuntimeError("history state did not reach a terminal phase")
        telemetry["state"] = {
            "phase": persisted.phase,
            "cursor": persisted.target_cursor,
            "total_targets": persisted.total_targets,
            "cycle_count": persisted.cycle_count,
        }

        # Repetition: it must never redownload a ZIP nor rewrite prior evidence or notifications.
        clock_state[0] = clock_state[0] + timedelta(minutes=1)
        before_notifications = [str(item.notification_id) for item in notifications.items]
        with LocalStorage(StoragePaths.from_root(workspace), read_only=True) as storage:
            before_reports = storage.raw_records.count(
                schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION
            )
        baseline = transport.snapshot()
        runtime = ApplicationRuntime.create_default()
        application = SecInstitutionalHistoryApplication(
            runtime,
            transport_factory=lambda: transport,
            clock=lambda: clock_state[0],
        )
        repeat = application.run_cycle(
            SecInstitutionalHistoryRequest(known_at=clock_state[0]),
            sec_identity=sec_identity,
            location=location,
            outbox_state=outbox_state,
        )
        repeat_calls = transport.delta(baseline)
        repeated_notifications = CazatiburonesNotificationStore(outbox_state).load()
        repeated_ids = [str(item.notification_id) for item in repeated_notifications.items]
        with LocalStorage(StoragePaths.from_root(workspace), read_only=True) as storage:
            after_reports = storage.raw_records.count(
                schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION
            )
        telemetry["repeat"] = {
            "status": repeat.status,
            "phase": repeat.phase,
            "cursor_before": repeat.target_cursor_before,
            "cursor_after": repeat.target_cursor_after,
            "zip_calls": repeat.zip_calls,
            "submissions_calls": repeat.submissions_calls,
            "calls": repeat_calls,
            "reports_before": before_reports,
            "reports_after": after_reports,
            "prior_notifications_preserved": set(before_notifications).issubset(set(repeated_ids)),
            "outbox_items": len(repeated_ids),
        }
        if repeat_calls["zip"]:
            raise RuntimeError("a repetition redownloaded a ZIP dataset")
        if repeat_calls["third_party"]:
            raise RuntimeError("unexpected third-party network call during repetition")
        if not telemetry["repeat"]["prior_notifications_preserved"]:
            raise RuntimeError("a repetition rewrote previously persisted notifications")
        if after_reports < before_reports:
            raise RuntimeError("a repetition removed previously persisted evidence")
        if repeat_calls["submissions"]:
            if (
                repeat.target is None
                or repeat.target_cursor_after != repeat.target_cursor_before + 1
            ):
                raise RuntimeError("a repetition consumed Submissions without advancing one target")
            if repeat_calls["submissions"] != 1:
                raise RuntimeError("a repetition performed more than one Submissions GET")
        elif repeat.target_cursor_after != repeat.target_cursor_before:
            raise RuntimeError("a repetition without Submissions advanced the target cursor")

        telemetry["total_calls"] = transport.snapshot()

    telemetry["status"] = "ok" if observed_comparison else "no-comparable-pair"
    sys.stdout.write(json.dumps(telemetry, indent=2) + "\n")
    return 0 if observed_comparison else 2


if __name__ == "__main__":
    smoke_telemetry: dict[str, object] = {}
    try:
        raise SystemExit(main(smoke_telemetry))
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - publish partial evidence before failing
        smoke_telemetry["status"] = "failed"
        smoke_telemetry["error"] = f"{type(error).__name__}: {error}"
        sys.stdout.write(json.dumps(smoke_telemetry, indent=2) + "\n")
        sys.stderr.write(f"smoke failed: {type(error).__name__}: {error}\n")
        raise
