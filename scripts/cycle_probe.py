#!/usr/bin/env python3
"""Captura fotografias comparables del ciclo diario de investment-analyst.

Modos:
  baseline  -> escribe una foto del estado actual (se ejecuta antes del ciclo)
  report    -> espera a que el ciclo del dia termine, toma otra foto y la compara

No escribe nada en el workspace: abre DuckDB en read_only y lee archivos de estado.
"""

from __future__ import annotations

import collections
import datetime as dt
import importlib.util
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from zoneinfo import ZoneInfo

WORKSPACE = pathlib.Path("/home/marjuraru/.local/share/investment-analyst/workspaces/default")
DB = WORKSPACE / "storage/data/processed/investment_analyst.duckdb"
STATE = WORKSPACE / "state"
JOURNAL = STATE / "multi_asset_schedule_state_v1_journal"
ARTIFACT = STATE / "storage_observability_v1.jsonl"
OPS = pathlib.Path("/home/marjuraru/.local/share/investment-analyst/ops")
SAMPLES = OPS / "samples"
REPORTS = OPS / "reports"
OVERVIEW_URL = "http://127.0.0.1:8765/api/v1/overview"
LIMA = ZoneInfo("America/Lima")

CGROUP = pathlib.Path(
    "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
    "/app.slice/investment-analyst.service"
)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - una sonda nunca aborta por una parte
        return {"error": f"{type(error).__name__}: {error}"} if default is None else default


def overview() -> dict:
    with urllib.request.urlopen(OVERVIEW_URL, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def journal_records(journal_dir: pathlib.Path | None = None) -> list[dict]:
    target_journal = journal_dir or JOURNAL
    records: list[dict] = []
    snapshot_path = target_journal / "snapshot.json"
    if snapshot_path.exists():
        records.extend(json.loads(snapshot_path.read_text(encoding="utf-8"))["records"])
    for segment in sorted(target_journal.glob("segment-*.jsonl")):
        for line in segment.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                records.append(entry.get("payload") or entry.get("record") or entry)
    latest_by_attempt: dict[str, dict] = {}
    unkeyed: list[dict] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        attempt_id = item.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id:
            # Snapshot plus later journal transitions can contain the same lifecycle.
            latest_by_attempt[attempt_id] = item
        else:
            unkeyed.append(item)
    unique = [*latest_by_attempt.values(), *unkeyed]
    unique.sort(
        key=lambda item: (
            str(item.get("started_at") or ""),
            str(
                item["definition"].get("job_id") or ""
                if isinstance(item.get("definition"), Mapping)
                else ""
            ),
            _integer(item.get("attempt_number")) or 0,
            str(item.get("attempt_id") or ""),
        )
    )
    return unique


def _seconds(record: dict) -> float | None:
    started = record.get("started_at") or (record.get("execution") or {}).get("started_at")
    completed = record.get("completed_at")
    if not started or not completed:
        return None
    start_utc = _utc(started)
    end_utc = _utc(completed)
    if start_utc is None or end_utc is None:
        return None
    return (end_utc - start_utc).total_seconds()


def _parse_local_date(value: object) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _attempt_local_date(record: Mapping[str, object]) -> dt.date | None:
    explicit = _parse_local_date(record.get("local_date"))
    if explicit is not None:
        return explicit
    raw_started = record.get("started_at")
    if raw_started is None and isinstance(record.get("execution"), Mapping):
        raw_started = record["execution"].get("started_at")
    started = _utc(raw_started if isinstance(raw_started, (str, dt.datetime)) else None)
    return started.astimezone(LIMA).date() if started is not None else None


def _artifact_local_date(record: Mapping[str, object]) -> dt.date | None:
    return _parse_local_date(record.get("local_date"))


def cycle(day: str, journal_dir: pathlib.Path | None = None) -> dict:
    target_day = dt.date.fromisoformat(day)
    records = [r for r in journal_records(journal_dir) if _attempt_local_date(r) == target_day]
    jobs = []
    for record in records:
        definition = record.get("definition") or {}
        raw_failure = record.get("failure")
        failure = None
        if isinstance(raw_failure, Mapping):
            failure = {
                "category": raw_failure.get("category"),
                "reason_code": raw_failure.get("reason_code"),
            }
        jobs.append(
            {
                "job_id": definition.get("job_id") or definition.get("key"),
                "attempt_id": record.get("attempt_id"),
                "attempt_number": record.get("attempt_number"),
                "local_date": record.get("local_date") or day,
                "status": record.get("status"),
                "seconds": _seconds(record),
                "started_at": record.get("started_at")
                or (record.get("execution") or {}).get("started_at"),
                "completed_at": record.get("completed_at"),
                "failure": failure,
            }
        )
    jobs.sort(key=lambda item: item["seconds"] or 0, reverse=True)
    starts = [j["started_at"] for j in jobs if j["started_at"]]
    ends = [j["completed_at"] for j in jobs if j["completed_at"]]
    wall = None
    if starts and ends:
        start_utc_list = [_utc(s) for s in starts if _utc(s) is not None]
        end_utc_list = [_utc(e) for e in ends if _utc(e) is not None]
        if start_utc_list and end_utc_list:
            wall = (max(end_utc_list) - min(start_utc_list)).total_seconds()
    return {
        "day": day,
        "attempts": len(jobs),
        "by_status": dict(collections.Counter(j["status"] for j in jobs)),
        "failures": [j for j in jobs if j["failure"]],
        "wall_seconds": wall,
        "sum_seconds": sum(j["seconds"] or 0 for j in jobs),
        "slowest": jobs[:15],
        "all_jobs": jobs,
    }


def observability(day: str, artifact_path: pathlib.Path | None = None) -> dict:
    target_day = dt.date.fromisoformat(day)
    target_artifact = artifact_path or ARTIFACT
    if not target_artifact.exists():
        return {"present": False}
    rows = []
    for line in target_artifact.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    today = [r for r in rows if _artifact_local_date(r) == target_day]
    return {
        "present": True,
        "total_lines": len(rows),
        "schema_versions": dict(collections.Counter(r.get("schema_version") for r in rows)),
        "today_count": len(today),
        "today": [
            {
                "job_id": r.get("job_id"),
                "attempt_id": r.get("attempt_id"),
                "attempt_number": r.get("attempt_number"),
                "attempt_status": r.get("attempt_status"),
                "failure_category": r.get("failure_category"),
                "failure_reason_code": r.get("failure_reason_code"),
                "rows_created": r.get("rows_created"),
                "rows_reused": r.get("rows_reused"),
                "collector_overhead_ms": r.get("collector_overhead_ms"),
                "durations": r.get("durations"),
                "database_bytes_before": r.get("database_bytes_before"),
                "database_bytes_after": r.get("database_bytes_after"),
                "growth": r.get("growth"),
            }
            for r in today
        ],
    }


def database(db_path: pathlib.Path | None = None) -> dict:
    import duckdb

    target_db = db_path or DB
    connection = duckdb.connect(str(target_db), read_only=True)
    connection.execute("set threads=2")
    connection.execute("set memory_limit='512MB'")
    try:
        by_key = connection.execute(
            "select metric_key, count(*), sum(strlen(document_json)) "
            "from metric_results group by 1 order by 3 desc"
        ).fetchall()
        tables = connection.execute(
            "select table_name, estimated_size from duckdb_tables() order by 2 desc"
        ).fetchall()
        windows = connection.execute(
            "select metric_key, json_extract_string(document_json,'$.parameters.window'), "
            "count(*), sum(strlen(document_json)) from metric_results "
            "where metric_key like 'crypto.derivatives%' group by 1,2 order by 4 desc"
        ).fetchall()
        recent = connection.execute(
            "select inserted_at::date, count(*), sum(strlen(document_json)) "
            "from metric_results where inserted_at >= current_date - 3 group by 1 order by 1 desc"
        ).fetchall()
    finally:
        connection.close()
    return {
        "file_bytes": target_db.stat().st_size,
        "metric_rows": sum(r[1] for r in by_key),
        "metric_document_bytes": sum(r[2] or 0 for r in by_key),
        "by_metric_key": [{"metric_key": k, "rows": n, "bytes": b or 0} for k, n, b in by_key],
        "tables": [{"table": t, "rows": n} for t, n in tables],
        "crypto_by_window": [
            {"metric_key": k, "window": w, "rows": n, "bytes": b or 0} for k, w, n, b in windows
        ],
        "recent_growth": [{"date": str(d), "rows": n, "bytes": b or 0} for d, n, b in recent],
    }


def containment(cgroup_path: pathlib.Path | None = None) -> dict:
    target_cgroup = cgroup_path or CGROUP

    def _read(name: str) -> str | None:
        path = target_cgroup / name
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    properties = (
        subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "investment-analyst",
                "--property=MemoryPeak,MemoryCurrent,MemoryHigh,MemoryMax,MemorySwapMax,CPUUsageNSec,"
                "ActiveState,ExecMainStartTimestamp,WorkingDirectory",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        .stdout.strip()
        .splitlines()
    )
    return {
        "unit": dict(line.split("=", 1) for line in properties if "=" in line),
        "memory_events": _read("memory.events"),
        "memory_pressure": _read("memory.pressure"),
        "memory_peak_bytes": _read("memory.peak"),
    }


def institutional_cursor(state_dir: pathlib.Path | None = None) -> dict:
    target_state = state_dir or STATE
    hits = {}
    for path in target_state.glob("*institutional*"):
        if path.is_file() and path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            hits[path.name] = {
                k: v
                for k, v in payload.items()
                if "cursor" in k or "total" in k or "phase" in k or "status" in k
            }
    return hits


def _load_memory_sampler():
    module_name = "_cycle_probe_memory_sampler"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    sampler_path = pathlib.Path(__file__).with_name("memory_sampler.py")
    spec = importlib.util.spec_from_file_location(module_name, sampler_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("memory sampler is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _last_report_metadata(reports_dir: pathlib.Path) -> dict[str, object]:
    candidates = sorted(reports_dir.glob("cycle-*.json"), key=lambda path: path.name)
    if not candidates:
        return {"available": False, "reason": "no_cycle_report"}
    path = candidates[-1]
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {"available": False, "reason": "report_metadata_too_large"}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "report_metadata_unreadable"}
    if not isinstance(payload, Mapping):
        return {"available": False, "reason": "report_metadata_invalid"}
    identity = payload.get("runtime_identity")
    observed = identity.get("observed") if isinstance(identity, Mapping) else None
    wait = payload.get("wait")
    if not isinstance(observed, Mapping):
        return {
            "available": False,
            "reason": "report_runtime_identity_missing",
            "report": path.name,
        }
    return {
        "available": True,
        "report": path.name,
        "day": payload.get("day"),
        "cycle_completed": wait.get("completed") is True if isinstance(wait, Mapping) else False,
        "release_sha": observed.get("release_sha"),
        "release_sha_state": observed.get("release_sha_state"),
        "pid": observed.get("pid"),
        "process_starttime_ticks": observed.get("process_starttime_ticks"),
        "boot_id": observed.get("boot_id"),
        "cgroup_path": observed.get("cgroup_path"),
        "cgroup_generation": observed.get("cgroup_generation"),
    }


def runtime_identity_read_only(
    *,
    reports_dir: pathlib.Path | None = None,
    sample: object | None = None,
) -> dict[str, object]:
    """Compare live systemd/proc/cgroup identity to the last report metadata only."""
    sampler = _load_memory_sampler()
    current_sample = sample if sample is not None else sampler.capture_sample()
    observed = sampler.runtime_identity_summary(current_sample)
    reference = _last_report_metadata(reports_dir or REPORTS)
    if reference.get("available") is not True:
        comparison: dict[str, object] = {"state": "inconclusive", "reason": reference.get("reason")}
    else:
        release_sha = observed.get("release_sha")
        reference_sha = reference.get("release_sha")
        release_match = (
            release_sha == reference_sha
            if isinstance(release_sha, str) and isinstance(reference_sha, str)
            else None
        )
        comparable_process = all(
            observed.get(key) is not None and reference.get(key) is not None
            for key in ("pid", "process_starttime_ticks", "boot_id", "cgroup_generation")
        )
        same_process = (
            all(
                observed.get(key) == reference.get(key)
                for key in ("pid", "process_starttime_ticks", "boot_id", "cgroup_generation")
            )
            if comparable_process
            else None
        )
        comparison = {
            "state": "inconclusive"
            if release_match is None or same_process is None
            else "match"
            if release_match and same_process
            else "changed",
            "release_match": release_match,
            "same_process_and_cgroup_generation": same_process,
        }
    return {
        "schema_version": "runtime-identity-read-only-v1",
        "observed": observed,
        "reference": reference,
        "comparison": comparison,
        "workspace_accessed": False,
        "database_accessed": False,
        "writes_performed": False,
    }


def snapshot(day: str) -> dict:
    return {
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "day": day,
        "runtime_identity": _safe(lambda: runtime_identity_read_only(), {}),
        "overview": _safe(overview),
        "cycle": _safe(lambda: cycle(day)),
        "observability": _safe(lambda: observability(day)),
        "database": _safe(database),
        "containment": _safe(containment),
        "institutional": _safe(institutional_cursor),
    }


def with_memory(payload: dict, *, samples_dir: pathlib.Path | None = None) -> dict:
    cycle_data = payload.get("cycle") or {}
    observability_data = payload.get("observability") or {}
    observation_rows = observability_data.get("today", [])
    payload["memory"] = _safe(
        lambda: memory_by_job(
            payload["day"],
            cycle_data.get("all_jobs", []),
            samples_dir=samples_dir,
            observability_rows=observation_rows if isinstance(observation_rows, list) else [],
        )
    )
    return payload


def local_day() -> str:
    return dt.datetime.now(LIMA).date().isoformat()


def wait_for_cycle(
    day: str,
    *,
    timeout_seconds: float = 6 * 3600,
    poll_seconds: float = 60,
    overview_fn: Callable[[], dict] | None = None,
    now_fn: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Wait for a scheduler overview that proves no run or retry remains today."""
    if timeout_seconds < 0 or poll_seconds <= 0:
        raise ValueError("cycle wait timing must be non-negative with a positive poll")
    target_day = dt.date.fromisoformat(day)
    interval_start = dt.datetime.combine(target_day, dt.time.min, tzinfo=LIMA).astimezone(dt.UTC)
    interval_end = dt.datetime.combine(
        target_day + dt.timedelta(days=1), dt.time.min, tzinfo=LIMA
    ).astimezone(dt.UTC)
    read_overview = overview_fn or overview
    deadline = monotonic_fn() + timeout_seconds
    log: list[dict[str, object]] = []
    last_status: dict[str, object] | None = None

    while True:
        observed_at = now_fn()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("cycle wait clock must be timezone-aware")
        try:
            current = read_overview()
        except Exception:  # noqa: BLE001 - keep the probe safe and bounded
            current = None
        last_status = current if isinstance(current, dict) else None

        scheduler_enabled = last_status.get("scheduler_enabled") is True if last_status else False
        job_count = last_status.get("scheduled_job_count") if last_status else None
        running_count = last_status.get("scheduled_running_count") if last_status else None
        next_run = _utc(
            last_status.get("scheduled_next_run_at")
            if isinstance(last_status, Mapping)
            and isinstance(last_status.get("scheduled_next_run_at"), (str, dt.datetime))
            else None
        )
        next_retry = _utc(
            last_status.get("scheduled_next_retry_at")
            if isinstance(last_status, Mapping)
            and isinstance(last_status.get("scheduled_next_retry_at"), (str, dt.datetime))
            else None
        )
        has_target_run = any(
            value is not None and interval_start <= value < interval_end
            for value in (next_run, next_retry)
        )
        counts_valid = (
            isinstance(job_count, int)
            and not isinstance(job_count, bool)
            and job_count > 0
            and isinstance(running_count, int)
            and not isinstance(running_count, bool)
            and running_count >= 0
        )
        ready = scheduler_enabled and counts_valid and running_count == 0 and not has_target_run
        log.append(
            {
                "observed_at": observed_at.astimezone(dt.UTC).isoformat(),
                "scheduler_enabled": scheduler_enabled,
                "scheduled_job_count": job_count,
                "scheduled_running_count": running_count,
                "next_run_in_target_day": next_run is not None
                and interval_start <= next_run < interval_end,
                "next_retry_in_target_day": next_retry is not None
                and interval_start <= next_retry < interval_end,
                "overview_available": last_status is not None,
            }
        )
        if ready:
            return {
                "completed": True,
                "day": target_day.isoformat(),
                "observed_at": observed_at.astimezone(dt.UTC).isoformat(),
                "scheduled_job_count": job_count,
                "scheduled_running_count": running_count,
                "log": log[-40:],
            }
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            reason = "overview_unavailable" if last_status is None else "timeout"
            return {
                "completed": False,
                "reason": reason,
                "day": target_day.isoformat(),
                "last_overview": last_status,
                "log": log[-40:],
            }
        sleep_fn(min(poll_seconds, remaining))


def render_summary(payload: dict) -> str:
    """Render measurements with explicit dates, identities, and uncertainty."""
    base = payload.get("baseline") or {}
    lines = [f"# Ciclo {payload['day']}", ""]
    wait = payload.get("wait", {})
    lines.append(
        "- Espera al ciclo: "
        + ("completada" if wait.get("completed") else "AGOTADA — informe parcial")
    )
    lines.append(
        "- Ejecución independiente del candidato: no se lanzó hoy; se observó el runtime activo."
    )

    def identity_sha(source: Mapping[str, object]) -> str | None:
        identity = source.get("runtime_identity")
        observed = identity.get("observed") if isinstance(identity, Mapping) else None
        sha = observed.get("release_sha") if isinstance(observed, Mapping) else None
        return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None

    current_sha = identity_sha(payload)
    reference_sha = identity_sha(base) if base else None
    same_release = current_sha is not None and current_sha == reference_sha
    reference_day = base.get("day") if base else None
    reference_file = payload.get("baseline_file")
    if reference_file:
        comparison = (
            f"{reference_file} ({reference_day}); release {current_sha} coincide"
            if same_release
            else f"{reference_file} ({reference_day}); release no comparable"
        )
    else:
        comparison = "sin ciclo anterior cerrado con release conocido"
    lines.append(f"- Referencia: {comparison}.")

    def cyc(source):
        value = source.get("cycle") or {}
        return value if isinstance(value, dict) and "attempts" in value else {}

    now, before = cyc(payload), cyc(base)
    if now:
        wall_before = round((before.get("wall_seconds") or 0) / 60, 1) if same_release else "-"
        wall_now = round((now.get("wall_seconds") or 0) / 60, 1)
        sum_before = round((before.get("sum_seconds") or 0) / 60, 1) if same_release else "-"
        sum_now = round((now.get("sum_seconds") or 0) / 60, 1)
        ref_label = reference_day or "referencia comparable"
        before_attempts = before.get("attempts", "-") if same_release else "-"
        before_failures = len(before.get("failures", [])) if same_release else "-"
        lines += [
            "",
            "## Ciclo",
            "",
            f"| | {ref_label} | {payload['day']} |",
            "|---|---|---|",
            f"| intentos | {before_attempts} | {now.get('attempts', '-')} |",
            f"| fallos | {before_failures} | {len(now.get('failures', []))} |",
            f"| pared (min) | {wall_before} | {wall_now} |",
            f"| suma de duraciones (min) | {sum_before} | {sum_now} |",
            "",
            "### Intentos más lentos (min)",
            "",
        ]
        previous = (
            {
                j["job_id"]: j.get("seconds") or 0
                for j in before.get("all_jobs", [])
                if isinstance(j, Mapping) and j.get("job_id")
            }
            if same_release
            else {}
        )
        lines += [f"| intento | {ref_label} | {payload['day']} |", "|---|---|---|"]
        for job in now.get("slowest", [])[:10]:
            was = previous.get(job["job_id"])
            attempt = job.get("attempt_number")
            attempt_label = f"{job['job_id']} #{attempt}" if attempt else job["job_id"]
            lines.append(
                f"| `{attempt_label}` | {round(was / 60, 1) if was is not None else '-'} "
                f"| {round((job.get('seconds') or 0) / 60, 1)} |"
            )
        failures = now.get("failures", [])
        if failures:
            lines += ["", "### Fallos", ""]
            for failure in failures:
                details = failure.get("failure") or {}
                category = details.get("category") or "categoría desconocida"
                reason = details.get("reason_code") or "sin reason_code"
                attempt = failure.get("attempt_number")
                lines.append(
                    f"- `{failure['job_id']} #{attempt}`: categoría `{category}`, razón `{reason}`"
                )

    obs = payload.get("observability") or {}
    if obs.get("present"):
        lines += [
            "",
            "## Observabilidad",
            "",
            f"- versiones del artefacto: {obs['schema_versions']}",
            "",
        ]
        lines += [
            "| intento | estado | filas C/R | etapas ms | overhead ms | fallo seguro |",
            "|---|---|---|---|---|---|",
        ]
        for item in sorted(
            obs.get("today", []),
            key=lambda r: (r.get("durations") or {}).get("job_execution_ms") or 0,
            reverse=True,
        )[:10]:
            durations = item.get("durations") or {}
            attempt = item.get("attempt_number")
            failure_label = (
                "/".join(
                    value
                    for value in (item.get("failure_category"), item.get("failure_reason_code"))
                    if isinstance(value, str) and value
                )
                or "-"
            )
            lines.append(
                f"| `{item['job_id']} #{attempt or '-'}` | {item.get('attempt_status')} "
                f"| {item.get('rows_created')}/{item.get('rows_reused')} "
                f"| `{json.dumps(durations, sort_keys=True, separators=(',', ':'))}` "
                f"| {item.get('collector_overhead_ms')} | `{failure_label}` |"
            )

    db_now, db_before = payload.get("database") or {}, base.get("database") or {}
    if "metric_rows" in db_now:
        file_gb_before = round((db_before.get("file_bytes") or 0) / 1e9, 2) if same_release else "-"
        file_gb_now = round(db_now["file_bytes"] / 1e9, 2)
        doc_gb_before = (
            round((db_before.get("metric_document_bytes") or 0) / 1e9, 2) if same_release else "-"
        )
        doc_gb_now = round(db_now["metric_document_bytes"] / 1e9, 2)
        rows_before = (
            f"{db_before['metric_rows']:,}" if same_release and "metric_rows" in db_before else "-"
        )
        lines += [
            "",
            "## Almacenamiento",
            "",
            f"| | {reference_day or 'referencia comparable'} | {payload['day']} |",
            "|---|---|---|",
            f"| archivo (GB) | {file_gb_before} | {file_gb_now} |",
            f"| filas de metricas | {rows_before} | {db_now['metric_rows']:,} |",
            f"| bytes de documento (GB) | {doc_gb_before} | {doc_gb_now} |",
            "",
            "### Ventanas cripto (cambio frente al ciclo de referencia)",
            "",
            f"| clave | ventana | {reference_day or 'referencia comparable'} "
            f"| {payload['day']} | delta |",
            "|---|---|---|---|---|",
        ]
        previous_windows = (
            {
                (w["metric_key"], w["window"]): w["rows"]
                for w in db_before.get("crypto_by_window", [])
            }
            if same_release
            else {}
        )
        for window in db_now.get("crypto_by_window", []):
            was = previous_windows.get((window["metric_key"], window["window"]))
            delta = window["rows"] - was if was is not None else None
            mark = " **<-- debería ser 0**" if window["window"] in {"720", "30"} and delta else ""
            lines.append(
                f"| `{window['metric_key']}` | {window['window']} | "
                f"{was:,} | {window['rows']:,} | {delta:+,}{mark} |"
                if was is not None
                else (
                    f"| `{window['metric_key']}` | {window['window']} | - "
                    f"| {window['rows']:,} | - |"
                )
            )

    containment_data = payload.get("containment") or {}
    if "memory_events" in containment_data:
        lines += [
            "",
            "## Contencion",
            "",
            f"- eventos: `{str(containment_data.get('memory_events')).replace(chr(10), ' | ')}`",
            f"- pico de memoria: {containment_data.get('memory_peak_bytes')}",
            f"- presion: `{str(containment_data.get('memory_pressure')).replace(chr(10), ' | ')}`",
        ]

    memory = payload.get("memory") or {}
    if memory:
        lines += ["", "## Memoria por trabajo (observada por intento)", ""]
        lines.append(
            f"- captura: {memory.get('capture_kind')}; comparable: {memory.get('comparable')} "
            f"({memory.get('comparison_reason') or 'identidad coherente'}); release: "
            f"{memory.get('release_sha') or memory.get('release_identity_state')}"
        )
        if memory.get("rss_peak") is not None:
            peak_gb = round((memory.get("rss_peak") or 0) / 1e9, 2)
            lines.append(f"- pico RSS: {peak_gb} GB a las {memory.get('rss_peak_at')}")
        else:
            lines.append("- pico RSS: sin muestras durante el intervalo del ciclo")

        jobs_during_peak = memory.get("jobs_during_peak") or []
        if memory.get("job_during_peak"):
            lines.append(f"- trabajo en curso durante el pico: `{memory['job_during_peak']}`")
        elif len(jobs_during_peak) > 1:
            peak_jobs_str = ", ".join(f"`{j}`" for j in jobs_during_peak)
            lines.append(f"- trabajos en curso durante el pico (solapamiento): {peak_jobs_str}")
        else:
            lines.append("- trabajo en curso durante el pico: ninguno")

        uncovered_count = len(memory.get("jobs_without_samples") or [])
        lines += [
            "",
            f"- muestras: {memory.get('sample_count')} cada "
            f"{memory.get('sample_interval_seconds')} s",
            f"- trabajos sin muestra en su intervalo: {uncovered_count}",
            f"- muestras sin intento simultáneo: {memory.get('unattributed_sample_count', 0)}",
            "",
            "Las cifras son observaciones temporales; no atribuyen memoria causada al intento.",
            "Cada delta exige PID, inicio de proceso, boot y cgroup estables.",
            "Sin identidad completa, el delta queda nulo.",
            "Los contadores `high`, `max` y `oom` son eventos acumulados, no segundos.",
            "",
            "| intento | estado/cobertura | muestras | RSS max GB | RSS Δ MB | "
            "memory.current Δ MB | memory.peak Δ MB | high Δ | fallo |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for item in memory.get("by_job", [])[:12]:
            net_str = (
                f"{round(item['rss_net'] / 1e6, 1)}" if item.get("rss_net") is not None else "-"
            )
            max_str = (
                f"{round(item['rss_max'] / 1e9, 2)}" if item.get("rss_max") is not None else "-"
            )
            high_str = (
                str(item.get("high_events_delta"))
                if item.get("high_events_delta") is not None
                else f"- ({item.get('high_events_reason')})"
            )
            current_str = (
                f"{round(item['memory_current_delta_bytes'] / 1e6, 1)}"
                if item.get("memory_current_delta_bytes") is not None
                else f"- ({item.get('memory_current_delta_reason')})"
            )
            peak_delta_str = (
                f"{round(item['memory_peak_delta_bytes'] / 1e6, 1)}"
                if item.get("memory_peak_delta_bytes") is not None
                else f"- ({item.get('memory_peak_delta_reason')})"
            )
            attempt_label = f"{item['job_id']} #{item.get('attempt_number') or '-'}"
            failure_label = (
                "/".join(
                    value
                    for value in (item.get("failure_category"), item.get("failure_reason_code"))
                    if isinstance(value, str) and value
                )
                or "-"
            )
            lines.append(
                f"| `{attempt_label}` | {item.get('status')}/{item.get('coverage')} "
                f"| {item.get('samples')} "
                f"| {max_str} "
                f"| {net_str} "
                f"| {current_str} "
                f"| {peak_delta_str} "
                f"| {high_str} | `{failure_label}` |"
            )
        if memory.get("intervals_without_jobs"):
            lines += ["", "Intervalos observados sin intento activo:", ""]
            for interval in memory["intervals_without_jobs"]:
                lines.append(
                    f"- {interval['started_at']}–{interval['completed_at']}: "
                    f"{interval['seconds']} s, {interval['sample_count']} muestras"
                )

    lines += ["", "## 13F", "", f"```\n{json.dumps(payload.get('institutional'), indent=2)}\n```"]
    return "\n".join(lines) + "\n"


def _utc(value: str | dt.datetime | None) -> dt.datetime | None:
    """Instante consciente de zona, normalizado a UTC. Nunca se comparan cadenas."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


_MEMORY_IDENTITY_FIELDS = (
    "pid",
    "process_starttime_ticks",
    "boot_id",
    "cgroup_generation",
)


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _first_present(record: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def _normalize_memory_sample(record: object) -> dict[str, object] | None:
    if not isinstance(record, Mapping):
        return None
    moment = _utc(
        _first_present(record, "at", "timestamp", "sampled_at", "captured_at")
        if isinstance(
            _first_present(record, "at", "timestamp", "sampled_at", "captured_at"),
            (str, dt.datetime),
        )
        else None
    )
    if moment is None:
        return None
    rss = _integer(_first_present(record, "VmRSS", "vm_rss_bytes", "rss_bytes", "rss"))
    if rss is None:
        rss_kb = _integer(_first_present(record, "vm_rss_kb", "rss_kb"))
        rss = rss_kb * 1024 if rss_kb is not None else None
    events = record.get("memory_events")
    event_map = events if isinstance(events, Mapping) else {}
    pid = _integer(_first_present(record, "pid", "process_id"))
    starttime = _integer(_first_present(record, "process_starttime_ticks", "starttime_ticks"))
    output: dict[str, object] = {
        "_at": moment,
        "pid": pid if pid and pid > 0 else None,
        "process_starttime_ticks": starttime,
        "boot_id": _first_present(record, "boot_id", "boot_identity"),
        "cgroup_generation": _first_present(record, "cgroup_generation", "cgroup_identity"),
        "cgroup_path": _first_present(record, "cgroup_path", "ControlGroup"),
        "release_sha": _first_present(record, "release_sha", "observed_sha"),
        "release_sha_state": record.get("release_sha_state"),
        "VmRSS": rss,
        "VmHWM": _integer(_first_present(record, "VmHWM", "vm_hwm_bytes", "rss_peak_bytes")),
        "memory_current_bytes": _integer(
            _first_present(
                record,
                "memory_current_bytes",
                "memory_current",
                "memory.current",
                "cgroup_memory_current_bytes",
            )
        ),
        "memory_peak_bytes": _integer(
            _first_present(
                record,
                "memory_peak_bytes",
                "memory_peak",
                "memory.peak",
                "cgroup_memory_peak_bytes",
            )
        ),
        "high_events": _integer(
            _first_present(record, "high_events", "memory_high_events")
            if _first_present(record, "high_events", "memory_high_events") is not None
            else event_map.get("high")
        ),
        "max_events": _integer(
            _first_present(record, "max_events", "memory_max_events")
            if _first_present(record, "max_events", "memory_max_events") is not None
            else event_map.get("max")
        ),
        "oom_events": _integer(
            _first_present(record, "oom_events", "memory_oom_events")
            if _first_present(record, "oom_events", "memory_oom_events") is not None
            else event_map.get("oom")
        ),
        "oom_kill_events": _integer(
            _first_present(record, "oom_kill_events", "memory_oom_kill_events")
            if _first_present(record, "oom_kill_events", "memory_oom_kill_events") is not None
            else event_map.get("oom_kill")
        ),
        "sample_interval_seconds": _integer(
            _first_present(record, "sample_interval_seconds", "interval_seconds")
        ),
    }
    for field in ("boot_id", "cgroup_generation", "release_sha", "cgroup_path"):
        if not isinstance(output[field], str) or not output[field]:
            output[field] = None
    return output


def _sample_cadence(samples: list[dict[str, object]]) -> float:
    declared = [
        int(sample["sample_interval_seconds"])
        for sample in samples
        if isinstance(sample.get("sample_interval_seconds"), int)
        and not isinstance(sample.get("sample_interval_seconds"), bool)
        and int(sample["sample_interval_seconds"]) > 0
    ]
    if declared:
        return float(statistics.median(declared))
    return 5.0


def _safe_memory_delta(
    samples: list[dict[str, object]], field: str, *, counter: bool
) -> tuple[int | None, str | None]:
    measured = [sample for sample in samples if isinstance(sample.get(field), int)]
    if not measured:
        return None, "no_samples"
    if len(measured) < 2:
        return None, "insufficient_samples"
    if any(
        not all(sample.get(identity) is not None for identity in _MEMORY_IDENTITY_FIELDS)
        for sample in measured
    ):
        return None, "identity_incomplete"
    identity_reason = {
        "pid": "pid_changed",
        "process_starttime_ticks": "process_restarted",
        "boot_id": "boot_changed",
        "cgroup_generation": "cgroup_generation_changed",
    }
    for identity, reason in identity_reason.items():
        if len({sample[identity] for sample in measured}) > 1:
            return None, reason
    values = [int(sample[field]) for sample in measured]
    if counter and any(values[index + 1] < values[index] for index in range(len(values) - 1)):
        return None, "counter_reset"
    return values[-1] - values[0], None


def _unoccupied_intervals(
    start_at: dt.datetime | None,
    end_at: dt.datetime | None,
    jobs: list[tuple[dict, dt.datetime, dt.datetime]],
    samples: list[dict[str, object]],
) -> list[dict[str, object]]:
    if start_at is None or end_at is None:
        return []
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = start_at
    for _, job_start, job_end in sorted(jobs, key=lambda item: item[1]):
        if job_start > cursor:
            intervals.append((cursor, min(job_start, end_at)))
        cursor = max(cursor, job_end)
        if cursor >= end_at:
            break
    if cursor < end_at:
        intervals.append((cursor, end_at))
    return [
        {
            "started_at": interval_start.isoformat(),
            "completed_at": interval_end.isoformat(),
            "seconds": round((interval_end - interval_start).total_seconds(), 1),
            "sample_count": sum(
                interval_start <= sample["_at"] <= interval_end for sample in samples
            ),
        }
        for interval_start, interval_end in intervals
        if interval_end > interval_start
    ]


def memory_by_job(
    day: str,
    jobs: list[dict],
    *,
    samples_dir: pathlib.Path | None = None,
    samples_data: list[dict] | None = None,
    observability_rows: list[dict] | None = None,
) -> dict:
    """Compare sample series to individual durable attempts without causal attribution."""
    if samples_data is not None:
        raw_samples = list(samples_data)
    else:
        target_samples_dir = samples_dir or SAMPLES
        target_day = dt.date.fromisoformat(day)
        paths = [
            target_samples_dir / f"mem-{(target_day - dt.timedelta(days=1)).isoformat()}.jsonl",
            target_samples_dir / f"mem-{day}.jsonl",
            target_samples_dir / f"mem-{(target_day + dt.timedelta(days=1)).isoformat()}.jsonl",
        ]
        raw_samples = []
        for path in paths:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    raw_samples.append(json.loads(line))
                except ValueError:
                    continue

    samples: list[dict[str, object]] = []
    for item in raw_samples:
        normalized = _normalize_memory_sample(item)
        if normalized is not None:
            samples.append(normalized)

    samples.sort(key=lambda item: item["_at"])

    valid_jobs: list[tuple[dict, dt.datetime, dt.datetime]] = []
    for job in jobs:
        raw_start = job.get("started_at") or (job.get("execution") or {}).get("started_at")
        raw_end = job.get("completed_at")
        start_at = _utc(raw_start)
        end_at = _utc(raw_end)
        if start_at is not None and end_at is not None and start_at <= end_at:
            valid_jobs.append((job, start_at, end_at))

    if valid_jobs:
        cycle_start = min(s for _, s, _ in valid_jobs)
        cycle_end = max(e for _, _, e in valid_jobs)
    else:
        cycle_start = None
        cycle_end = None

    cycle_samples = (
        [s for s in samples if cycle_start <= s["_at"] <= cycle_end]
        if cycle_start is not None and cycle_end is not None
        else []
    )
    rss_cycle_samples = [s for s in cycle_samples if s.get("VmRSS") is not None]

    if rss_cycle_samples:
        peak_sample = max(rss_cycle_samples, key=lambda s: s["VmRSS"])
        peak_at = peak_sample["_at"]
        rss_peak = peak_sample["VmRSS"]
        active_at_peak = [
            job["job_id"]
            for job, start_at, end_at in valid_jobs
            if start_at <= peak_at <= end_at and job.get("job_id")
        ]
        jobs_during_peak = sorted(dict.fromkeys(active_at_peak))
        job_during_peak = jobs_during_peak[0] if len(jobs_during_peak) == 1 else None
    else:
        peak_sample = None
        peak_at = None
        rss_peak = None
        jobs_during_peak = []
        job_during_peak = None

    attributed = []
    for job, start_at, end_at in valid_jobs:
        job_duration = (end_at - start_at).total_seconds()
        window = [s for s in samples if start_at <= s["_at"] <= end_at]
        interval_seconds = _sample_cadence(window)

        if not window:
            coverage = "none"
            start_gap = None
            end_gap = None
            max_internal_gap = None
            max_gap = round(job_duration, 1)
        else:
            start_gap = (window[0]["_at"] - start_at).total_seconds()
            end_gap = (end_at - window[-1]["_at"]).total_seconds()
            if len(window) >= 2:
                internal_gaps = [
                    (window[i + 1]["_at"] - window[i]["_at"]).total_seconds()
                    for i in range(len(window) - 1)
                ]
                max_internal_gap = max(internal_gaps)
                max_gap = round(max([start_gap, end_gap, max_internal_gap]), 1)
            else:
                max_internal_gap = 0.0
                max_gap = round(max(start_gap, end_gap), 1)

            if (
                start_gap <= interval_seconds * 2
                and end_gap <= interval_seconds * 2
                and max_internal_gap <= interval_seconds * 3
            ):
                coverage = "complete"
            else:
                coverage = "partial"

        rss = [int(s["VmRSS"]) for s in window if s.get("VmRSS") is not None]
        current = [
            int(s["memory_current_bytes"])
            for s in window
            if s.get("memory_current_bytes") is not None
        ]
        peak = [
            int(s["memory_peak_bytes"]) for s in window if s.get("memory_peak_bytes") is not None
        ]
        pids = sorted({int(s["pid"]) for s in window if s.get("pid") is not None})
        deltas = {
            name: _safe_memory_delta(
                window,
                name,
                counter=name
                in {
                    "memory_peak_bytes",
                    "high_events",
                    "max_events",
                    "oom_events",
                    "oom_kill_events",
                },
            )
            for name in (
                "VmRSS",
                "memory_current_bytes",
                "memory_peak_bytes",
                "high_events",
                "max_events",
                "oom_events",
                "oom_kill_events",
            )
        }
        observed = next(
            (
                row
                for row in observability_rows or []
                if job.get("attempt_id") is not None
                and row.get("attempt_id") == job.get("attempt_id")
            ),
            {},
        )

        attributed.append(
            {
                "job_id": job["job_id"],
                "attempt_id": job.get("attempt_id"),
                "attempt_number": job.get("attempt_number"),
                "status": job.get("status"),
                "failure": job.get("failure"),
                "failure_category": (job.get("failure") or {}).get("category")
                if isinstance(job.get("failure"), Mapping)
                else None,
                "failure_reason_code": (job.get("failure") or {}).get("reason_code")
                if isinstance(job.get("failure"), Mapping)
                else None,
                "seconds": job.get("seconds"),
                "samples": len(window),
                "coverage": coverage,
                "covered": coverage != "none",
                "cadence_seconds": interval_seconds,
                "start_gap_seconds": round(start_gap, 1) if start_gap is not None else None,
                "end_gap_seconds": round(end_gap, 1) if end_gap is not None else None,
                "max_gap_seconds": max_gap,
                "pids": pids,
                "rss_first": rss[0] if rss else None,
                "rss_last": rss[-1] if rss else None,
                "rss_max": max(rss) if rss else None,
                "rss_spread": max(rss) - min(rss) if rss else None,
                "rss_net": deltas["VmRSS"][0],
                "rss_net_reason": deltas["VmRSS"][1],
                "memory_current_first_bytes": current[0] if current else None,
                "memory_current_last_bytes": current[-1] if current else None,
                "memory_current_delta_bytes": deltas["memory_current_bytes"][0],
                "memory_current_delta_reason": deltas["memory_current_bytes"][1],
                "memory_peak_max_bytes": max(peak) if peak else None,
                "memory_peak_delta_bytes": deltas["memory_peak_bytes"][0],
                "memory_peak_delta_reason": deltas["memory_peak_bytes"][1],
                "high_events_delta": deltas["high_events"][0],
                "high_events_reason": deltas["high_events"][1],
                "max_events_delta": deltas["max_events"][0],
                "max_events_reason": deltas["max_events"][1],
                "oom_events_delta": deltas["oom_events"][0],
                "oom_events_reason": deltas["oom_events"][1],
                "oom_kill_events_delta": deltas["oom_kill_events"][0],
                "oom_kill_events_reason": deltas["oom_kill_events"][1],
                "collector_durations_ms": observed.get("durations"),
                "collector_overhead_ms": observed.get("collector_overhead_ms"),
            }
        )

    attributed.sort(
        key=lambda item: item["rss_max"] if item.get("rss_max") is not None else -1,
        reverse=True,
    )
    uncovered = [
        item.get("attempt_id") or item["job_id"]
        for item in attributed
        if item["coverage"] == "none"
    ]
    unattributed_samples = [
        sample
        for sample in cycle_samples
        if not any(start <= sample["_at"] <= end for _, start, end in valid_jobs)
    ]
    runtime_identities = {
        tuple(sample.get(field) for field in _MEMORY_IDENTITY_FIELDS)
        for sample in samples
        if all(sample.get(field) is not None for field in _MEMORY_IDENTITY_FIELDS)
    }
    complete_identity_samples = sum(
        all(sample.get(field) is not None for field in _MEMORY_IDENTITY_FIELDS)
        for sample in samples
    )
    release_shas = sorted(
        {
            sample["release_sha"]
            for sample in samples
            if isinstance(sample.get("release_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", sample["release_sha"])
        }
    )
    release_complete = bool(samples) and all(
        sample.get("release_sha") in release_shas for sample in samples
    )
    if len(release_shas) > 1:
        release_state = "mixed"
    elif release_complete and release_shas:
        release_state = "known"
    elif any(sample.get("release_sha_state") == "incoherent" for sample in samples):
        release_state = "incoherent"
    else:
        release_state = "unknown"
    if len(samples) < 2:
        comparison_reason = "single_point"
    elif release_state != "known":
        comparison_reason = "release_identity_unavailable"
    elif complete_identity_samples != len(samples):
        comparison_reason = "process_cgroup_identity_incomplete"
    elif len(runtime_identities) != 1:
        comparison_reason = "process_or_cgroup_identity_changed"
    else:
        comparison_reason = None
    intervals_without_jobs = _unoccupied_intervals(
        cycle_start, cycle_end, valid_jobs, cycle_samples
    )
    sample_intervals = [
        int(sample["sample_interval_seconds"])
        for sample in samples
        if isinstance(sample.get("sample_interval_seconds"), int)
        and not isinstance(sample.get("sample_interval_seconds"), bool)
        and int(sample["sample_interval_seconds"]) > 0
    ]
    return {
        "present": bool(samples),
        "reason": None if samples else "no_valid_samples",
        "capture_kind": "series" if len(samples) >= 2 else "point" if samples else "none",
        "comparable": comparison_reason is None,
        "comparison_reason": comparison_reason,
        "release_sha": release_shas[0] if release_state == "known" else None,
        "release_shas": release_shas,
        "release_identity_state": release_state,
        "process_identity_count": len(runtime_identities),
        "process_identity_complete_sample_count": complete_identity_samples,
        "sample_count": len(samples),
        "cycle_sample_count": len(cycle_samples),
        "sample_interval_seconds": (
            int(statistics.median(sample_intervals)) if sample_intervals else 5
        ),
        "first_sample_at": samples[0]["_at"].isoformat() if samples else None,
        "last_sample_at": samples[-1]["_at"].isoformat() if samples else None,
        "cycle_start_at": cycle_start.isoformat() if cycle_start else None,
        "cycle_end_at": cycle_end.isoformat() if cycle_end else None,
        "rss_peak": rss_peak,
        "rss_peak_at": peak_at.isoformat() if peak_at else None,
        "job_during_peak": job_during_peak,
        "jobs_during_peak": jobs_during_peak,
        "jobs_without_samples": uncovered,
        "unattributed_sample_count": len(unattributed_samples),
        "unattributed_samples": [sample["_at"].isoformat() for sample in unattributed_samples],
        "intervals_without_jobs": intervals_without_jobs,
        "by_job": attributed[:100],
    }


def previous_snapshot(day: str, reports_dir: pathlib.Path | None = None) -> tuple[str, dict] | None:
    """Select the latest earlier completed cycle with an exact known release identity."""
    target_day = dt.date.fromisoformat(day)
    target_reports = reports_dir or REPORTS
    candidates: list[tuple[dt.date, pathlib.Path, dict]] = []
    for path in target_reports.glob("cycle-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        report_day = _parse_local_date(payload.get("day"))
        wait = payload.get("wait")
        identity = payload.get("runtime_identity")
        observed = identity.get("observed") if isinstance(identity, Mapping) else None
        release_sha = observed.get("release_sha") if isinstance(observed, Mapping) else None
        if (
            report_day is None
            or report_day >= target_day
            or not isinstance(wait, Mapping)
            or wait.get("completed") is not True
            or not isinstance(release_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", release_sha) is None
        ):
            continue
        candidates.append((report_day, path, payload))
    if not candidates:
        return None
    _, chosen, payload = max(candidates, key=lambda item: (item[0], item[1].name))
    return chosen.name, payload


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = arguments[0] if arguments else "report"
    if mode == "runtime-identity":
        try:
            result = runtime_identity_read_only()
        except Exception as error:  # noqa: BLE001 - emit safe partial probe failure
            result = {
                "schema_version": "runtime-identity-read-only-v1",
                "state": "unavailable",
                "reason": type(error).__name__,
                "workspace_accessed": False,
                "database_accessed": False,
                "writes_performed": False,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    REPORTS.mkdir(parents=True, exist_ok=True)
    if mode == "baseline":
        day = local_day()
        payload = with_memory(snapshot(day))
        payload["mode"] = "baseline"
        target = REPORTS / f"baseline-{day}.json"
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"baseline escrito en {target}")
        return 0

    day = local_day()
    waited = wait_for_cycle(day)
    payload = with_memory(snapshot(day))
    payload["mode"] = "report"
    payload["wait"] = waited
    reference = previous_snapshot(day)
    if reference is not None:
        name, content = reference
        content.pop("baseline", None)
        payload["baseline_file"] = name
        payload["baseline"] = content
    target = REPORTS / f"cycle-{day}.json"
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    summary = REPORTS / f"cycle-{day}.md"
    summary.write_text(render_summary(payload), encoding="utf-8")
    print(f"informe escrito en {target} y {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
