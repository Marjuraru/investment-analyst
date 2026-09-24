"""Pruebas unitarias para la atribucion temporal de memoria en scripts/cycle_probe.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

# Cargar dinamicamente scripts/cycle_probe.py como modulo para pruebas unitarias.
SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cycle_probe.py"
spec = importlib.util.spec_from_file_location("cycle_probe", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
cycle_probe = importlib.util.module_from_spec(spec)
sys.modules["cycle_probe"] = cycle_probe
spec.loader.exec_module(cycle_probe)


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
            {
                "at": f"2026-09-24T07:00:{sec:02d}Z",
                "VmRSS": 100_000_000 + sec * 100_000,
                "pid": 501,
                "high_events": 10 + sec // 5,
            }
        )

    # 2. job-gap-start: primera muestra a 07:10:15 (gap inicio = 15s > 10s)
    samples.append(
        {
            "at": "2026-09-24T07:10:15Z",
            "VmRSS": 200_000_000,
            "pid": 502,
            "high_events": 20,
        }
    )
    samples.append(
        {
            "at": "2026-09-24T07:10:35Z",
            "VmRSS": 210_000_000,
            "pid": 502,
            "high_events": 20,
        }
    )

    # 3. job-gap-internal: muestras a 07:20:05, salto a 07:20:30 (hueco 25s > 15s), luego 07:20:55
    samples.append(
        {
            "at": "2026-09-24T07:20:05Z",
            "VmRSS": 300_000_000,
            "pid": 503,
            "high_events": 30,
        }
    )
    samples.append(
        {
            "at": "2026-09-24T07:20:30Z",
            "VmRSS": 310_000_000,
            "pid": 503,
            "high_events": 30,
        }
    )
    samples.append(
        {
            "at": "2026-09-24T07:20:55Z",
            "VmRSS": 320_000_000,
            "pid": 503,
            "high_events": 30,
        }
    )

    # 4. job-uncovered: ninguna muestra entre 07:30 y 07:31

    # 5. job-pid-change: cambio de PID de 601 a 602
    samples.append(
        {
            "at": "2026-09-24T07:40:05Z",
            "VmRSS": 400_000_000,
            "pid": 601,
            "high_events": 40,
        }
    )
    samples.append(
        {
            "at": "2026-09-24T07:40:25Z",
            "VmRSS": 420_000_000,
            "pid": 602,
            "high_events": 40,
        }
    )

    # 6. job-counter-reset: contador cgroup disminuye de 50 a 5
    samples.append(
        {
            "at": "2026-09-24T07:50:05Z",
            "VmRSS": 500_000_000,
            "pid": 701,
            "high_events": 50,
        }
    )
    samples.append(
        {
            "at": "2026-09-24T07:50:25Z",
            "VmRSS": 510_000_000,
            "pid": 701,
            "high_events": 5,
        }
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
    assert res["by_job"] == []

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
        {
            "at": "2026-09-24T07:07:00Z",
            "VmRSS": 2_500_000_000,
            "pid": 200,
            "high_events": 15,
        }
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

    # 3. high_events_delta es un conteo, no segundos:
    for item in res["by_job"]:
        assert item["high_events_delta"] == 0  # 15 - 15 = 0 eventos

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
    assert "son un contador acumulado, no segundos" in summary.lower()
