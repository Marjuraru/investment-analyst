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
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

WORKSPACE = pathlib.Path("/home/marjuraru/.local/share/investment-analyst/workspaces/default")
DB = WORKSPACE / "storage/data/processed/investment_analyst.duckdb"
STATE = WORKSPACE / "state"
JOURNAL = STATE / "multi_asset_schedule_state_v1_journal"
ARTIFACT = STATE / "storage_observability_v1.jsonl"
OPS = pathlib.Path("/home/marjuraru/.local/share/investment-analyst/ops")
SAMPLES = OPS / "samples"
REPORTS = OPS / "reports"
OVERVIEW_URL = "http://127.0.0.1:8765/api/v1/overview"

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
    return [item for item in records if isinstance(item, dict)]


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


def cycle(day: str, journal_dir: pathlib.Path | None = None) -> dict:
    records = [
        r for r in journal_records(journal_dir) if (r.get("completed_at") or "").startswith(day)
    ]
    jobs = []
    for record in records:
        definition = record.get("definition") or {}
        jobs.append(
            {
                "job_id": definition.get("job_id") or definition.get("key"),
                "status": record.get("status"),
                "seconds": _seconds(record),
                "started_at": record.get("started_at")
                or (record.get("execution") or {}).get("started_at"),
                "completed_at": record.get("completed_at"),
                "failure": record.get("failure"),
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
    target_artifact = artifact_path or ARTIFACT
    if not target_artifact.exists():
        return {"present": False}
    rows = []
    for line in target_artifact.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    today = [r for r in rows if str(r.get("local_date", "")).startswith(day)]
    return {
        "present": True,
        "total_lines": len(rows),
        "schema_versions": dict(collections.Counter(r.get("schema_version") for r in rows)),
        "today_count": len(today),
        "today": [
            {
                "job_id": r.get("job_id"),
                "attempt_status": r.get("attempt_status"),
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


def snapshot(day: str) -> dict:
    return {
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "day": day,
        "overview": _safe(overview),
        "cycle": _safe(lambda: cycle(day)),
        "observability": _safe(lambda: observability(day)),
        "database": _safe(database),
        "containment": _safe(containment),
        "institutional": _safe(institutional_cursor),
    }


def with_memory(payload: dict, *, samples_dir: pathlib.Path | None = None) -> dict:
    cycle_data = payload.get("cycle") or {}
    payload["memory"] = _safe(
        lambda: memory_by_job(
            payload["day"],
            cycle_data.get("all_jobs", []),
            samples_dir=samples_dir,
        )
    )
    return payload


def local_day() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d")


def wait_for_cycle(day: str, *, timeout_seconds: int = 6 * 3600) -> dict:
    """Espera a que el ciclo del dia termine: sin trabajos corriendo y cuenta estable."""
    deadline = time.time() + timeout_seconds
    stable = 0
    previous = -1
    log: list[str] = []
    while time.time() < deadline:
        current = _safe(overview, {})
        running = current.get("scheduled_running_count")
        attempts = len(
            [r for r in _safe(journal_records, []) if (r.get("completed_at") or "").startswith(day)]
        )
        log.append(
            f"{dt.datetime.now().isoformat(timespec='seconds')} "
            f"running={running} attempts={attempts}"
        )
        if running == 0 and attempts >= 50 and attempts == previous:
            stable += 1
            if stable >= 3:
                return {"completed": True, "attempts": attempts, "log": log[-40:]}
        else:
            stable = 0
        previous = attempts
        time.sleep(120)
    return {"completed": False, "reason": "timeout", "log": log[-40:]}


def render_summary(payload: dict) -> str:
    """Resumen legible con la comparacion contra la linea base."""
    base = payload.get("baseline") or {}
    lines = [f"# Ciclo {payload['day']}", ""]
    wait = payload.get("wait", {})
    lines.append(
        "- Espera al ciclo: "
        + ("completada" if wait.get("completed") else "AGOTADA — informe parcial")
    )

    def cyc(source):
        value = source.get("cycle") or {}
        return value if isinstance(value, dict) and "attempts" in value else {}

    now, before = cyc(payload), cyc(base)
    if now:
        wall_before = round((before.get("wall_seconds") or 0) / 60, 1)
        wall_now = round((now.get("wall_seconds") or 0) / 60, 1)
        sum_before = round((before.get("sum_seconds") or 0) / 60, 1)
        sum_now = round((now.get("sum_seconds") or 0) / 60, 1)
        lines += [
            "",
            "## Ciclo",
            "",
            "| | ayer | hoy |",
            "|---|---|---|",
            f"| intentos | {before.get('attempts', '-')} | {now.get('attempts', '-')} |",
            f"| fallos | {len(before.get('failures', []))} | {len(now.get('failures', []))} |",
            f"| pared (min) | {wall_before} | {wall_now} |",
            f"| suma de duraciones (min) | {sum_before} | {sum_now} |",
            "",
            "### Trabajos mas lentos (min)",
            "",
        ]
        previous = {j["job_id"]: j.get("seconds") or 0 for j in before.get("all_jobs", [])}
        lines += ["| job | ayer | hoy |", "|---|---|---|"]
        for job in now.get("slowest", [])[:10]:
            was = previous.get(job["job_id"])
            lines.append(
                f"| `{job['job_id']}` | {round(was / 60, 1) if was is not None else '-'} "
                f"| {round((job.get('seconds') or 0) / 60, 1)} |"
            )
        failures = now.get("failures", [])
        if failures:
            lines += ["", "### Fallos", ""]
            for failure in failures:
                lines.append(f"- `{failure['job_id']}`: {failure['failure']}")

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
            "| job | creadas | reusadas | job_execution_ms | overhead colector ms |",
            "|---|---|---|---|---|",
        ]
        for item in sorted(
            obs.get("today", []),
            key=lambda r: (
                (r.get("durations") or {}).get("job_execution_ms")
                or (r.get("durations") or {}).get("network_ms")
                or 0
            ),
            reverse=True,
        )[:10]:
            durations = item.get("durations") or {}
            lines.append(
                f"| `{item['job_id']}` | {item.get('rows_created')} | {item.get('rows_reused')} "
                f"| {durations.get('job_execution_ms', durations.get('network_ms'))} "
                f"| {item.get('collector_overhead_ms')} |"
            )

    db_now, db_before = payload.get("database") or {}, base.get("database") or {}
    if "metric_rows" in db_now:
        file_gb_before = round((db_before.get("file_bytes") or 0) / 1e9, 2)
        file_gb_now = round(db_now["file_bytes"] / 1e9, 2)
        doc_gb_before = round((db_before.get("metric_document_bytes") or 0) / 1e9, 2)
        doc_gb_now = round(db_now["metric_document_bytes"] / 1e9, 2)
        rows_before = f"{db_before['metric_rows']:,}" if "metric_rows" in db_before else "-"
        lines += [
            "",
            "## Almacenamiento",
            "",
            "| | ayer | hoy |",
            "|---|---|---|",
            f"| archivo (GB) | {file_gb_before} | {file_gb_now} |",
            f"| filas de metricas | {rows_before} | {db_now['metric_rows']:,} |",
            f"| bytes de documento (GB) | {doc_gb_before} | {doc_gb_now} |",
            "",
            "### Ventanas cripto (filas nuevas respecto a ayer)",
            "",
            "| clave | ventana | ayer | hoy | delta |",
            "|---|---|---|---|---|",
        ]
        previous_windows = {
            (w["metric_key"], w["window"]): w["rows"] for w in db_before.get("crypto_by_window", [])
        }
        for window in db_now.get("crypto_by_window", []):
            was = previous_windows.get((window["metric_key"], window["window"]), 0)
            delta = window["rows"] - was
            mark = " **<-- deberia ser 0**" if window["window"] in {"720", "30"} and delta else ""
            lines.append(
                f"| `{window['metric_key']}` | {window['window']} | {was:,} | "
                f"{window['rows']:,} | {delta:+,}{mark} |"
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
    if memory.get("present"):
        lines += [
            "",
            "## Memoria por trabajo",
            "",
        ]
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
            "",
            "RSS max = maximo observado mientras el trabajo corria, **no** memoria causada",
            "por el trabajo. RSS neto = ultima menos primera muestra del intervalo. Los",
            "eventos `high` son un contador acumulado, no segundos de estrangulamiento.",
            "",
            "| job | s | muestras | RSS max (GB) | RSS neto (MB) | rango (MB) | eventos high |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in memory.get("by_job", [])[:12]:
            net_str = (
                f"{round(item['rss_net'] / 1e6, 1)}" if item.get("rss_net") is not None else "-"
            )
            spread_str = (
                f"{round(item['rss_spread'] / 1e6, 1)}"
                if item.get("rss_spread") is not None
                else "-"
            )
            max_str = (
                f"{round(item['rss_max'] / 1e9, 2)}" if item.get("rss_max") is not None else "-"
            )
            high_str = (
                str(item.get("high_events_delta"))
                if item.get("high_events_delta") is not None
                else "-"
            )
            lines.append(
                f"| `{item['job_id']}` | {round(item.get('seconds') or 0, 1)} "
                f"| {item.get('samples')} "
                f"| {max_str} "
                f"| {net_str} "
                f"| {spread_str} "
                f"| {high_str} |"
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


def memory_by_job(
    day: str,
    jobs: list[dict],
    *,
    samples_dir: pathlib.Path | None = None,
    samples_data: list[dict] | None = None,
) -> dict:
    """Cruza las muestras con el intervalo de cada trabajo, comparando instantes en UTC.

    `rss_max` es el maximo observado mientras el trabajo corria; no es memoria causada por el
    trabajo. `rss_net` es la diferencia entre la ultima y la primera muestra del intervalo
    unicamente con el mismo PID no nulo. `high_events_delta` es un contador, no segundos.
    """
    if samples_data is not None:
        raw_samples = list(samples_data)
    else:
        target_samples_dir = samples_dir or SAMPLES
        paths = [target_samples_dir / f"mem-{day}.jsonl"]
        previous_day = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
        paths.insert(0, target_samples_dir / f"mem-{previous_day}.jsonl")
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

    samples = []
    for item in raw_samples:
        moment = _utc(item.get("at"))
        if moment is None:
            continue
        parsed_item = dict(item)
        parsed_item["_at"] = moment
        samples.append(parsed_item)

    if not samples:
        return {"present": False, "reason": "sin muestras para el dia"}

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

    if cycle_start is not None and cycle_end is not None:
        cycle_samples = [
            s
            for s in samples
            if cycle_start <= s["_at"] <= cycle_end and s.get("VmRSS") is not None
        ]
    else:
        cycle_samples = []

    if cycle_samples:
        peak_sample = max(cycle_samples, key=lambda s: s["VmRSS"])
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

            if start_gap <= 10.0 and end_gap <= 10.0 and max_internal_gap <= 15.0:
                coverage = "complete"
            else:
                coverage = "partial"

        rss = [s["VmRSS"] for s in window if s.get("VmRSS") is not None]
        pids = sorted({s.get("pid") for s in window if s.get("pid") is not None})
        has_null_pid = any(s.get("pid") is None for s in window)

        if rss:
            rss_max = max(rss)
            rss_min = min(rss)
            rss_spread = rss_max - rss_min
            rss_first = rss[0]
            rss_last = rss[-1]
        else:
            rss_max = None
            rss_min = None
            rss_spread = None
            rss_first = None
            rss_last = None

        if not rss:
            rss_net = None
            rss_net_reason = "no_samples"
        elif len(pids) == 0 or has_null_pid:
            rss_net = None
            rss_net_reason = "null_pid"
        elif len(pids) > 1:
            rss_net = None
            rss_net_reason = "pid_changed"
        else:
            rss_net = rss_last - rss_first
            rss_net_reason = None

        high = [s["high_events"] for s in window if s.get("high_events") is not None]
        if not high:
            high_events_delta = None
            high_events_reason = "no_samples"
        elif any(high[i + 1] < high[i] for i in range(len(high) - 1)):
            high_events_delta = None
            high_events_reason = "counter_reset"
        else:
            high_events_delta = high[-1] - high[0]
            high_events_reason = None

        attributed.append(
            {
                "job_id": job["job_id"],
                "seconds": job.get("seconds"),
                "samples": len(window),
                "coverage": coverage,
                "covered": coverage != "none",
                "cadence_seconds": 5,
                "start_gap_seconds": round(start_gap, 1) if start_gap is not None else None,
                "end_gap_seconds": round(end_gap, 1) if end_gap is not None else None,
                "max_gap_seconds": max_gap,
                "pids": pids,
                "rss_first": rss_first,
                "rss_last": rss_last,
                "rss_max": rss_max,
                "rss_spread": rss_spread,
                "rss_net": rss_net,
                "rss_net_reason": rss_net_reason,
                "high_events_delta": high_events_delta,
                "high_events_reason": high_events_reason,
            }
        )

    attributed.sort(
        key=lambda item: item["rss_max"] if item.get("rss_max") is not None else -1,
        reverse=True,
    )
    uncovered = [item["job_id"] for item in attributed if item["coverage"] == "none"]
    return {
        "present": True,
        "sample_count": len(samples),
        "cycle_sample_count": len(cycle_samples),
        "sample_interval_seconds": 5,
        "first_sample_at": samples[0]["_at"].isoformat() if samples else None,
        "last_sample_at": samples[-1]["_at"].isoformat() if samples else None,
        "cycle_start_at": cycle_start.isoformat() if cycle_start else None,
        "cycle_end_at": cycle_end.isoformat() if cycle_end else None,
        "rss_peak": rss_peak,
        "rss_peak_at": peak_at.isoformat() if peak_at else None,
        "job_during_peak": job_during_peak,
        "jobs_during_peak": jobs_during_peak,
        "jobs_without_samples": uncovered,
        "by_job": [item for item in attributed if item["coverage"] != "none"][:20],
    }


def previous_snapshot(day: str, reports_dir: pathlib.Path | None = None) -> tuple[str, dict] | None:
    """Devuelve la foto mas reciente anterior a `day`, sea linea base o informe previo."""
    target_reports = reports_dir or REPORTS
    candidates: list[tuple[str, pathlib.Path]] = []
    for pattern in ("baseline-*.json", "cycle-*.json"):
        for path in target_reports.glob(pattern):
            stamp = path.stem.split("-", 1)[1]
            if stamp < day:
                candidates.append((stamp, path))
    if not candidates:
        return None
    candidates.sort()
    chosen = candidates[-1][1]
    try:
        return chosen.name, json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
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
