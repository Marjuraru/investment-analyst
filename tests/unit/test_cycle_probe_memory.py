"""Pruebas unitarias para la atribucion temporal de memoria en scripts/cycle_probe.py."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

# Cargar dinamicamente scripts/cycle_probe.py como modulo para pruebas unitarias.
SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cycle_probe.py"
spec = importlib.util.spec_from_file_location("cycle_probe", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
cycle_probe = importlib.util.module_from_spec(spec)
sys.modules["cycle_probe"] = cycle_probe
spec.loader.exec_module(cycle_probe)


def _with_identity(sample: dict, **identity_overrides: object) -> dict:
    identity = {
        "process_starttime_ticks": 1000,
        "boot_id": "boot-a",
        "cgroup_generation": "cgroup-a",
        "release_sha": "a" * 40,
        "release_sha_state": "known",
    }
    identity.update(identity_overrides)
    return {**sample, **identity}


def test_peak_is_within_cycle_and_lists_all_overlapping_jobs() -> None:
    """A1: un pico fuera del ciclo queda excluido; el pico dentro se normaliza a UTC

    y lista cero, uno o varios trabajos activos de forma determinista.
    """
    jobs = [
        {
            "job_id": "job-1",
            "started_at": "2026-09-24T07:00:00Z",
            "completed_at": "2026-09-24T07:10:00Z",
            "seconds": 600.0,
        },
        {
            "job_id": "job-2",
            "started_at": "2026-09-24T07:15:00Z",
            "completed_at": "2026-09-24T07:25:00Z",
            "seconds": 600.0,
        },
        {
            "job_id": "job-3",
            # En huso -05:00: 02:20:00-05:00 == 07:20:00Z
            "started_at": "2026-09-24T02:20:00-05:00",
            # En huso -05:00: 02:30:00-05:00 == 07:30:00Z
            "completed_at": "2026-09-24T02:30:00-05:00",
            "seconds": 600.0,
        },
    ]

    # Muestras sinteticas:
    # 1. Pico masivo ANTES del ciclo (06:45:00Z) -> 10 GB
    # 2. Pico masivo DESPUES del ciclo (08:00:00Z) -> 12 GB
    # 3. Muestra dentro del ciclo durante job-1 (07:05:00Z) -> 2 GB
    # 4. Muestra dentro del ciclo durante hueco (07:12:00Z) -> 1.5 GB
    # 5. Muestra dentro del ciclo durante solapamiento job-2 y job-3 (07:22:00Z) -> 3 GB
    samples = [
        {"at": "2026-09-24T06:45:00Z", "VmRSS": 10_000_000_000, "pid": 100},
        {"at": "2026-09-24T07:05:00Z", "VmRSS": 2_000_000_000, "pid": 100},
        {"at": "2026-09-24T07:12:00Z", "VmRSS": 1_500_000_000, "pid": 100},
        # Expresada en huso local -05:00: 02:22:00-05:00 == 07:22:00Z
        {"at": "2026-09-24T02:22:00-05:00", "VmRSS": 3_000_000_000, "pid": 100},
        {"at": "2026-09-24T08:00:00Z", "VmRSS": 12_000_000_000, "pid": 100},
    ]

    # Escenario con solapamiento: el pico del ciclo debe ser 3 GB (a las 07:22:00Z).
    # Los picos externos de 10 GB y 12 GB deben quedar absolutamente excluidos.
    res_overlap = cycle_probe.memory_by_job("2026-09-24", jobs, samples_data=samples)
    assert res_overlap["present"] is True
    assert res_overlap["rss_peak"] == 3_000_000_000
    assert res_overlap["rss_peak_at"] == "2026-09-24T07:22:00+00:00"
    # Solapamiento de job-2 y job-3 a las 07:22:00Z:
    # No elige un culpable arbitrario: job_during_peak es None y lista ambos ordenados.
    assert res_overlap["job_during_peak"] is None
    assert res_overlap["jobs_during_peak"] == ["job-2", "job-3"]

    # Escenario donde el pico cae en exactamente UN trabajo:
    samples_single = [
        {"at": "2026-09-24T06:50:00Z", "VmRSS": 9_000_000_000, "pid": 100},  # Fuera del ciclo
        {"at": "2026-09-24T07:05:00Z", "VmRSS": 4_000_000_000, "pid": 100},  # Dentro de job-1
        {"at": "2026-09-24T07:22:00Z", "VmRSS": 2_000_000_000, "pid": 100},
        {"at": "2026-09-24T08:10:00Z", "VmRSS": 15_000_000_000, "pid": 100},  # Fuera del ciclo
    ]
    res_single = cycle_probe.memory_by_job("2026-09-24", jobs, samples_data=samples_single)
    assert res_single["rss_peak"] == 4_000_000_000
    assert res_single["rss_peak_at"] == "2026-09-24T07:05:00+00:00"
    assert res_single["job_during_peak"] == "job-1"
    assert res_single["jobs_during_peak"] == ["job-1"]

    # Escenario donde el pico cae en un HUECO entre trabajos (07:12:00Z):
    samples_gap = [
        {"at": "2026-09-24T07:05:00Z", "VmRSS": 1_000_000_000, "pid": 100},
        {"at": "2026-09-24T07:12:00Z", "VmRSS": 5_000_000_000, "pid": 100},  # En hueco
        {"at": "2026-09-24T07:22:00Z", "VmRSS": 2_000_000_000, "pid": 100},
    ]
    res_gap = cycle_probe.memory_by_job("2026-09-24", jobs, samples_data=samples_gap)
    assert res_gap["rss_peak"] == 5_000_000_000
    assert res_gap["rss_peak_at"] == "2026-09-24T07:12:00+00:00"
    assert res_gap["job_during_peak"] is None
    assert res_gap["jobs_during_peak"] == []


def test_coverage_pid_and_counter_reset_are_explicit() -> None:
    """A2: cobertura none/partial/complete, reinicio de PID y reset de contador

    dejan los deltas en None con causa explicita; la cadencia y huecos constan.
    """
    jobs = [
        {
            "job_id": "job-complete",
            "started_at": "2026-09-24T07:00:00Z",
            "completed_at": "2026-09-24T07:00:50Z",
            "seconds": 50.0,
        },
        {
            "job_id": "job-gap-start",
            "started_at": "2026-09-24T07:10:00Z",
            "completed_at": "2026-09-24T07:10:40Z",
            "seconds": 40.0,
        },
        {
            "job_id": "job-gap-internal",
            "started_at": "2026-09-24T07:20:00Z",
            "completed_at": "2026-09-24T07:21:00Z",
            "seconds": 60.0,
        },
        {
            "job_id": "job-uncovered",
            "started_at": "2026-09-24T07:30:00Z",
            "completed_at": "2026-09-24T07:31:00Z",
            "seconds": 60.0,
        },
        {
            "job_id": "job-pid-change",
            "started_at": "2026-09-24T07:40:00Z",
            "completed_at": "2026-09-24T07:40:30Z",
            "seconds": 30.0,
        },
        {
            "job_id": "job-counter-reset",
            "started_at": "2026-09-24T07:50:00Z",
            "completed_at": "2026-09-24T07:50:30Z",
            "seconds": 30.0,
        },
    ]

    samples: list[dict] = []
    # 1. job-complete: muestras a 07:00:05, 07:00:10, 07:00:15 ... 07:00:45
    # Inicio gap = 5s <= 10s, Fin gap = 5s <= 10s, huecos internos = 5s <= 15s.
    for sec in range(5, 50, 5):
        samples.append(
            _with_identity(
                {
                    "at": f"2026-09-24T07:00:{sec:02d}Z",
                    "VmRSS": 100_000_000 + sec * 100_000,
                    "pid": 501,
                    "high_events": 10 + sec // 5,
                }
            )
        )

    # 2. job-gap-start: primera muestra a 07:10:15 (gap inicio = 15s > 10s)
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:10:15Z",
                "VmRSS": 200_000_000,
                "pid": 502,
                "high_events": 20,
            }
        )
    )
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:10:35Z",
                "VmRSS": 210_000_000,
                "pid": 502,
                "high_events": 20,
            }
        )
    )

    # 3. job-gap-internal: muestras a 07:20:05, salto a 07:20:30 (hueco 25s > 15s), luego 07:20:55
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:20:05Z",
                "VmRSS": 300_000_000,
                "pid": 503,
                "high_events": 30,
            }
        )
    )
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:20:30Z",
                "VmRSS": 310_000_000,
                "pid": 503,
                "high_events": 30,
            }
        )
    )
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:20:55Z",
                "VmRSS": 320_000_000,
                "pid": 503,
                "high_events": 30,
            }
        )
    )

    # 4. job-uncovered: ninguna muestra entre 07:30 y 07:31

    # 5. job-pid-change: cambio de PID de 601 a 602
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:40:05Z",
                "VmRSS": 400_000_000,
                "pid": 601,
                "high_events": 40,
            }
        )
    )
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:40:25Z",
                "VmRSS": 420_000_000,
                "pid": 602,
                "high_events": 40,
            }
        )
    )

    # 6. job-counter-reset: contador cgroup disminuye de 50 a 5
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:50:05Z",
                "VmRSS": 500_000_000,
                "pid": 701,
                "high_events": 50,
            }
        )
    )
    samples.append(
        _with_identity(
            {
                "at": "2026-09-24T07:50:25Z",
                "VmRSS": 510_000_000,
                "pid": 701,
                "high_events": 5,
            }
        )
    )

    result = cycle_probe.memory_by_job("2026-09-24", jobs, samples_data=samples)
    jobs_map = {item["job_id"]: item for item in result["by_job"]}

    # job-complete
    complete_item = jobs_map["job-complete"]
    assert complete_item["coverage"] == "complete"
    assert complete_item["covered"] is True
    assert complete_item["cadence_seconds"] == 5
    assert complete_item["max_gap_seconds"] <= 15.0
    assert complete_item["pids"] == [501]
    assert complete_item["rss_net"] == (100_000_000 + 45 * 100_000) - (100_000_000 + 5 * 100_000)
    assert complete_item["rss_net_reason"] is None
    assert complete_item["high_events_delta"] == 8
    assert complete_item["high_events_reason"] is None

    # job-gap-start
    gap_start_item = jobs_map["job-gap-start"]
    assert gap_start_item["coverage"] == "partial"
    assert gap_start_item["start_gap_seconds"] == 15.0

    # job-gap-internal
    gap_internal_item = jobs_map["job-gap-internal"]
    assert gap_internal_item["coverage"] == "partial"
    assert gap_internal_item["max_gap_seconds"] >= 25.0

    # job-uncovered
    assert "job-uncovered" in result["jobs_without_samples"]

    # job-pid-change
    pid_change_item = jobs_map["job-pid-change"]
    assert pid_change_item["rss_net"] is None
    assert pid_change_item["rss_net_reason"] == "pid_changed"
    assert pid_change_item["pids"] == [601, 602]

    # job-counter-reset
    counter_reset_item = jobs_map["job-counter-reset"]
    assert counter_reset_item["high_events_delta"] is None
    assert counter_reset_item["high_events_reason"] == "counter_reset"


def test_uncovered_cycle_and_non_memory_report_compatibility() -> None:
    """A3: captura sin muestras durante el ciclo expresa honestamente ausencia;

    los informes y secciones ajenas a memoria conservan su salida y estructura.
    """
    jobs = [
        {
            "job_id": "job-early-1",
            "started_at": "2026-09-24T07:00:00Z",
            "completed_at": "2026-09-24T07:15:00Z",
            "seconds": 900.0,
        },
        {
            "job_id": "job-early-2",
            "started_at": "2026-09-24T07:15:00Z",
            "completed_at": "2026-09-24T07:30:00Z",
            "seconds": 900.0,
        },
    ]
    # Muestras recolectadas a partir de las 10:35, despues del ciclo
    samples_late = [
        {"at": "2026-09-24T10:35:00Z", "VmRSS": 1_200_000_000, "pid": 999},
        {"at": "2026-09-24T10:35:05Z", "VmRSS": 1_250_000_000, "pid": 999},
        {"at": "2026-09-24T10:35:10Z", "VmRSS": 1_210_000_000, "pid": 999},
    ]

    res = cycle_probe.memory_by_job("2026-09-24", jobs, samples_data=samples_late)
    assert res["present"] is True
    assert res["sample_count"] == 3
    assert res["cycle_sample_count"] == 0
    assert res["rss_peak"] is None
    assert res["rss_peak_at"] is None
    assert res["job_during_peak"] is None
    assert res["jobs_during_peak"] == []
    assert set(res["jobs_without_samples"]) == {"job-early-1", "job-early-2"}
    assert {item["coverage"] for item in res["by_job"]} == {"none"}
    assert len(res["by_job"]) == 2

    # Compatibilidad de render_summary con ausencia de muestras en ciclo:
    mock_payload = {
        "day": "2026-09-24",
        "wait": {"completed": True},
        "cycle": {
            "attempts": 2,
            "failures": [],
            "wall_seconds": 1800.0,
            "sum_seconds": 1800.0,
            "slowest": jobs,
            "all_jobs": jobs,
        },
        "observability": {
            "present": True,
            "schema_versions": {"storage_observability_v1": 2},
            "today": [
                {
                    "job_id": "job-early-1",
                    "rows_created": 10,
                    "rows_reused": 5,
                    "durations": {"job_execution_ms": 120},
                    "collector_overhead_ms": 5,
                }
            ],
        },
        "database": {
            "file_bytes": 100_000_000,
            "metric_rows": 1000,
            "metric_document_bytes": 50_000_000,
            "crypto_by_window": [],
        },
        "containment": {
            "memory_events": "low 0\nhigh 0\nmax 0\noom 0",
            "memory_peak_bytes": "500000000",
            "memory_pressure": "some avg10=0.00",
        },
        "institutional": {},
        "memory": res,
    }

    summary = cycle_probe.render_summary(mock_payload)
    assert "# Ciclo 2026-09-24" in summary
    assert "## Ciclo" in summary
    assert "## Observabilidad" in summary
    assert "## Almacenamiento" in summary
    assert "## Contencion" in summary
    assert "## Memoria por trabajo" in summary
    assert "- pico RSS: sin muestras durante el intervalo del ciclo" in summary
    assert "- trabajo en curso durante el pico: ninguno" in summary
    assert "- trabajos sin muestra en su intervalo: 2" in summary
    assert "## 13F" in summary


def test_memory_probe_never_writes_workspace_or_calls_providers(tmp_path: pathlib.Path) -> None:
    """I1: la sonda no escribe en el workspace, no ejecuta scheduler ni providers,

    y no introduce dependencias ajenas.
    """
    # 1. Analisis estatico de importaciones en scripts/cycle_probe.py
    code = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "src.investment_analyst.providers" not in code
    assert "investment_analyst.services.scheduler" not in code
    assert "read_only=True" in code

    # 2. Prueba con workspace simulado
    fake_workspace = tmp_path / "workspace"
    fake_workspace.mkdir()
    fake_state = fake_workspace / "state"
    fake_state.mkdir()
    fake_journal = fake_state / "multi_asset_schedule_state_v1_journal"
    fake_journal.mkdir()

    # Guardar snapshot vacio
    snapshot_file = fake_journal / "snapshot.json"
    snapshot_file.write_text(json.dumps({"records": []}), encoding="utf-8")

    # Guardar mtimes antes de llamar
    mtimes_before = {p: p.stat().st_mtime_ns for p in fake_workspace.rglob("*")}

    records = cycle_probe.journal_records(fake_journal)
    assert records == []
    cyc = cycle_probe.cycle("2026-09-24", journal_dir=fake_journal)
    assert cyc["attempts"] == 0

    cursor = cycle_probe.institutional_cursor(fake_state)
    assert cursor == {}

    mtimes_after = {p: p.stat().st_mtime_ns for p in fake_workspace.rglob("*")}
    assert mtimes_before == mtimes_after


def test_overlap_and_counter_are_observations_not_causal_claims() -> None:
    """X1: ningun campo presenta RSS como consumo atribuido/causado ni eventos high

    como segundos. Solapamiento reporta lista completa sin culpables arbitrarios.
    """
    jobs = [
        {
            "job_id": "crypto.derivatives:binance",
            "started_at": "2026-09-24T07:00:00Z",
            "completed_at": "2026-09-24T07:10:00Z",
            "seconds": 600.0,
        },
        {
            "job_id": "crypto.derivatives:bybit",
            "started_at": "2026-09-24T07:05:00Z",
            "completed_at": "2026-09-24T07:15:00Z",
            "seconds": 600.0,
        },
    ]

    samples = [
        _with_identity(
            {
                "at": "2026-09-24T07:07:00Z",
                "VmRSS": 2_500_000_000,
                "pid": 200,
                "high_events": 15,
            }
        )
    ]

    res = cycle_probe.memory_by_job("2026-09-24", jobs, samples_data=samples)

    # 1. Solapamiento:
    assert res["job_during_peak"] is None
    assert res["jobs_during_peak"] == [
        "crypto.derivatives:binance",
        "crypto.derivatives:bybit",
    ]

    # 2. Las claves del resultado no contienen "consumed", "heap" ni inferencias causales:
    forbidden_terms = {"consumed", "heap", "caused", "attribution"}
    for item in res["by_job"]:
        for key in item:
            for term in forbidden_terms:
                assert term not in key.lower()

    # 3. No se calcula un delta con una sola muestra ni identidad incompleta.
    for item in res["by_job"]:
        assert item["high_events_delta"] is None
        assert item["high_events_reason"] == "insufficient_samples"

    # 4. El resumen legible declara explicitamente la naturaleza observacional:
    payload = {
        "day": "2026-09-24",
        "wait": {},
        "cycle": {"attempts": 2, "all_jobs": jobs},
        "memory": res,
    }
    summary = cycle_probe.render_summary(payload)
    expected_overlap_str = (
        "trabajos en curso durante el pico (solapamiento): "
        "`crypto.derivatives:binance`, `crypto.derivatives:bybit`"
    )
    assert expected_overlap_str in summary
    assert "memoria causada" in summary.lower()
    assert "son eventos acumulados, no segundos" in summary.lower()


def test_cycle_uses_lima_local_date_and_emits_only_safe_failure_fields(
    monkeypatch,
) -> None:
    records = [
        {
            "attempt_id": "a-1",
            "attempt_number": 1,
            "local_date": "2026-09-24",
            "definition": {"job_id": "job-explicit"},
            "status": "failed",
            "started_at": "2026-09-24T04:30:00Z",
            "completed_at": "2026-09-24T04:31:00Z",
            "failure": {
                "category": "provider_unavailable",
                "reason_code": "sec_submissions_fetch_failed",
                "message": "secret response body must not appear",
            },
        },
        {
            "attempt_id": "a-2",
            "attempt_number": 2,
            "definition": {"job_id": "job-legacy"},
            "status": "succeeded",
            "started_at": "2026-09-25T02:00:00Z",
            "completed_at": "2026-09-25T02:01:00Z",
        },
        {
            "attempt_id": "a-3",
            "local_date": "2026-09-23",
            "definition": {"job_id": "job-other-day"},
            "started_at": "2026-09-24T07:00:00Z",
            "completed_at": "2026-09-24T07:01:00Z",
        },
    ]
    monkeypatch.setattr(cycle_probe, "journal_records", lambda journal_dir=None: records)

    result = cycle_probe.cycle("2026-09-24")

    assert result["attempts"] == 2
    assert {job["job_id"] for job in result["all_jobs"]} == {"job-explicit", "job-legacy"}
    failed = next(job for job in result["all_jobs"] if job["job_id"] == "job-explicit")
    assert failed["attempt_id"] == "a-1"
    assert failed["attempt_number"] == 1
    assert failed["failure"] == {
        "category": "provider_unavailable",
        "reason_code": "sec_submissions_fetch_failed",
    }
    assert "secret" not in json.dumps(result)


def test_wait_for_cycle_uses_scheduler_state_and_lima_day_boundaries() -> None:
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    overview = {
        "scheduler_enabled": True,
        "scheduled_job_count": 24,
        "scheduled_running_count": 0,
        # Exactly the next Lima day's midnight; it is outside the requested interval.
        "scheduled_next_run_at": "2026-09-25T05:00:00Z",
        "scheduled_next_retry_at": None,
    }

    result = cycle_probe.wait_for_cycle(
        "2026-09-24",
        timeout_seconds=1,
        overview_fn=lambda: overview,
        now_fn=lambda: now,
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda seconds: None,
    )

    assert result["completed"] is True
    assert result["scheduled_job_count"] == 24
    assert result["log"][0]["next_run_in_target_day"] is False


def test_wait_for_cycle_returns_partial_when_retry_remains_inside_lima_day() -> None:
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    overview = {
        "scheduler_enabled": True,
        "scheduled_job_count": 24,
        "scheduled_running_count": 0,
        "scheduled_next_run_at": None,
        "scheduled_next_retry_at": "2026-09-25T04:59:00Z",
    }
    result = cycle_probe.wait_for_cycle(
        "2026-09-24",
        timeout_seconds=1,
        poll_seconds=0.5,
        overview_fn=lambda: overview,
        now_fn=lambda: datetime(2026, 9, 24, 12, tzinfo=UTC),
        monotonic_fn=lambda: elapsed[0],
        sleep_fn=sleep,
    )

    assert result["completed"] is False
    assert result["reason"] == "timeout"
    assert result["log"][-1]["next_retry_in_target_day"] is True
    assert elapsed[0] == 1


def test_v2_memory_deltas_require_full_runtime_identity_and_preserve_attempts() -> None:
    base = {
        "pid": 321,
        "process_starttime_ticks": 12345,
        "boot_id": "boot-a",
        "cgroup_generation": "cgroup-a",
        "release_sha": "a" * 40,
        "release_sha_state": "known",
        "sample_interval_seconds": 5,
    }
    jobs = [
        {
            "job_id": "sec-submissions",
            "attempt_id": "attempt-a",
            "attempt_number": 1,
            "status": "failed",
            "started_at": "2026-09-24T07:00:00Z",
            "completed_at": "2026-09-24T07:00:10Z",
            "failure": {"category": "provider_unavailable", "reason_code": "sec_fetch_failed"},
        },
        {
            "job_id": "sec-submissions",
            "attempt_id": "attempt-b",
            "attempt_number": 2,
            "status": "succeeded",
            "started_at": "2026-09-24T07:00:20Z",
            "completed_at": "2026-09-24T07:00:30Z",
        },
        {
            "job_id": "uncovered",
            "attempt_id": "attempt-c",
            "attempt_number": 1,
            "status": "succeeded",
            "started_at": "2026-09-24T07:01:00Z",
            "completed_at": "2026-09-24T07:01:10Z",
        },
    ]
    samples = [
        {
            **base,
            "at": "2026-09-24T07:00:02Z",
            "VmRSS": 1000,
            "memory_current_bytes": 900,
            "memory_peak_bytes": 1200,
            "memory_events": {"high": 4, "max": 1, "oom": 0, "oom_kill": 0},
        },
        {
            **base,
            "at": "2026-09-24T07:00:07Z",
            "VmRSS": 1200,
            "memory_current_bytes": 700,
            "memory_peak_bytes": 1500,
            "memory_events": {"high": 6, "max": 1, "oom": 0, "oom_kill": 0},
        },
        {
            **base,
            "at": "2026-09-24T07:00:22Z",
            "VmRSS": 1300,
            "memory_current_bytes": 800,
            "memory_peak_bytes": 1600,
            "memory_events": {"high": 6, "max": 1, "oom": 0, "oom_kill": 0},
        },
        {
            **base,
            "at": "2026-09-24T07:00:27Z",
            "VmRSS": 1400,
            "memory_current_bytes": 850,
            "memory_peak_bytes": 1800,
            "memory_events": {"high": 7, "max": 1, "oom": 0, "oom_kill": 0},
        },
    ]
    result = cycle_probe.memory_by_job(
        "2026-09-24",
        jobs,
        samples_data=samples,
        observability_rows=[
            {
                "attempt_id": "attempt-a",
                "durations": {"network_ms": 12, "persistence_ms": 3},
                "collector_overhead_ms": 2,
            }
        ],
    )

    by_attempt = {item["attempt_id"]: item for item in result["by_job"]}
    assert set(by_attempt) == {"attempt-a", "attempt-b", "attempt-c"}
    assert by_attempt["attempt-a"]["rss_net"] == 200
    assert by_attempt["attempt-a"]["memory_current_delta_bytes"] == -200
    assert by_attempt["attempt-a"]["memory_peak_delta_bytes"] == 300
    assert by_attempt["attempt-a"]["high_events_delta"] == 2
    assert by_attempt["attempt-a"]["collector_durations_ms"] == {
        "network_ms": 12,
        "persistence_ms": 3,
    }
    assert by_attempt["attempt-a"]["failure_reason_code"] == "sec_fetch_failed"
    assert by_attempt["attempt-b"]["high_events_delta"] == 1
    assert by_attempt["attempt-c"]["coverage"] == "none"
    assert result["jobs_without_samples"] == ["attempt-c"]
    assert result["capture_kind"] == "series"
    assert result["comparable"] is True
    assert result["release_sha"] == "a" * 40


def test_memory_series_refuses_deltas_across_process_cgroup_or_release_changes() -> None:
    job = {
        "job_id": "job",
        "attempt_id": "attempt",
        "attempt_number": 1,
        "started_at": "2026-09-24T07:00:00Z",
        "completed_at": "2026-09-24T07:00:10Z",
    }
    first = {
        "at": "2026-09-24T07:00:02Z",
        "pid": 1,
        "process_starttime_ticks": 10,
        "boot_id": "boot-a",
        "cgroup_generation": "group-a",
        "release_sha": "a" * 40,
        "VmRSS": 100,
        "memory_events": {"high": 9},
    }
    second = {
        **first,
        "at": "2026-09-24T07:00:07Z",
        "VmRSS": 200,
        "memory_events": {"high": 12},
    }
    legacy = {key: value for key, value in first.items() if key != "process_starttime_ticks"}
    legacy["at"] = "2026-09-24T07:00:07Z"
    for changed, reason in (
        ({**second, "pid": 2}, "pid_changed"),
        ({**second, "process_starttime_ticks": 11}, "process_restarted"),
        ({**second, "boot_id": "boot-b"}, "boot_changed"),
        ({**second, "cgroup_generation": "group-b"}, "cgroup_generation_changed"),
    ):
        entry = cycle_probe.memory_by_job("2026-09-24", [job], samples_data=[first, changed])[
            "by_job"
        ][0]
        assert entry["rss_net"] is None
        assert entry["rss_net_reason"] == reason
    legacy_result = cycle_probe.memory_by_job("2026-09-24", [job], samples_data=[first, legacy])[
        "by_job"
    ][0]
    assert legacy_result["rss_net"] is None
    assert legacy_result["rss_net_reason"] == "identity_incomplete"

    mixed_release = cycle_probe.memory_by_job(
        "2026-09-24", [job], samples_data=[first, {**second, "release_sha": "b" * 40}]
    )
    assert mixed_release["release_identity_state"] == "mixed"
    assert mixed_release["comparable"] is False
    assert mixed_release["comparison_reason"] == "release_identity_unavailable"


def test_previous_snapshot_skips_baselines_open_reports_and_unknown_releases(
    tmp_path: pathlib.Path,
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    sha_a = "a" * 40
    sha_b = "b" * 40
    (reports / "baseline-2026-10-02.json").write_text(
        json.dumps({"day": "2026-10-02", "runtime_identity": {"observed": {"release_sha": sha_a}}}),
        encoding="utf-8",
    )
    (reports / "cycle-2026-10-01.json").write_text(
        json.dumps(
            {
                "day": "2026-10-01",
                "wait": {"completed": True},
                "runtime_identity": {"observed": {"release_sha": sha_b}},
            }
        ),
        encoding="utf-8",
    )
    (reports / "cycle-2026-10-03.json").write_text(
        json.dumps(
            {
                "day": "2026-10-03",
                "wait": {"completed": False},
                "runtime_identity": {"observed": {"release_sha": sha_a}},
            }
        ),
        encoding="utf-8",
    )
    (reports / "cycle-2026-10-02.json").write_text(
        json.dumps(
            {
                "day": "2026-10-02",
                "wait": {"completed": True},
                "runtime_identity": {"observed": {"release_sha": None}},
            }
        ),
        encoding="utf-8",
    )

    chosen = cycle_probe.previous_snapshot("2026-10-04", reports)

    assert chosen is not None
    assert chosen[0] == "cycle-2026-10-01.json"
    assert chosen[1]["day"] == "2026-10-01"


def test_runtime_identity_probe_compares_metadata_without_writing(tmp_path: pathlib.Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    sha = "a" * 40
    report_path = reports / "cycle-2026-10-03.json"
    report_path.write_text(
        json.dumps(
            {
                "day": "2026-10-03",
                "wait": {"completed": True},
                "runtime_identity": {
                    "observed": {
                        "release_sha": sha,
                        "release_sha_state": "known",
                        "pid": 22,
                        "process_starttime_ticks": 500,
                        "boot_id": "boot-a",
                        "cgroup_path": "/user.slice/service",
                        "cgroup_generation": "generation-a",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    original_mtime = report_path.stat().st_mtime_ns
    sample = SimpleNamespace(
        at="2026-10-04T01:00:00Z",
        service="investment-analyst",
        pid=22,
        process_starttime_ticks=500,
        boot_id="boot-a",
        cgroup_path="/user.slice/service",
        cgroup_generation="generation-a",
        service_invocation_id="invocation",
        release_sha=sha,
        release_sha_state="known",
        memory_current_bytes=1,
        memory_peak_bytes=2,
        memory_high_limit_bytes=3,
        memory_max_limit_bytes=4,
        memory_swap_max_limit_bytes=None,
        missing_reasons={},
    )

    result = cycle_probe.runtime_identity_read_only(reports_dir=reports, sample=sample)

    assert result["comparison"]["state"] == "match"
    assert result["comparison"]["release_match"] is True
    assert result["workspace_accessed"] is False
    assert result["database_accessed"] is False
    assert result["writes_performed"] is False
    assert report_path.stat().st_mtime_ns == original_mtime
    assert list(reports.iterdir()) == [report_path]


def test_runtime_identity_cli_mode_does_not_create_report_directory(monkeypatch, tmp_path) -> None:
    target = tmp_path / "reports-do-not-create"
    monkeypatch.setattr(cycle_probe, "REPORTS", target)
    monkeypatch.setattr(
        cycle_probe,
        "runtime_identity_read_only",
        lambda: {"schema_version": "runtime-identity-read-only-v1", "writes_performed": False},
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert cycle_probe.main(["runtime-identity"]) == 0
    assert json.loads(output.getvalue())["writes_performed"] is False
    assert not target.exists()
