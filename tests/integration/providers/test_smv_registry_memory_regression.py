"""Hermetic memory regression coverage for the SMV registry pipeline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SMALL_CORPUS_ROWS = 50_000
_LARGE_CORPUS_ROWS = 100_000
_PEAK_DELTA_LIMIT = 100 * 1024 * 1024
_DUPLICATE_DELTA_LIMIT = 32 * 1024 * 1024

_MEMORY_WORKER = r'''
from __future__ import annotations

import gc
import json
import os
import resource
import sys
import tracemalloc
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from investment_analyst.providers.peru.smv_open_data import (
    SMV_COMPANIES_URL,
    SMV_SECURITIES_URL,
    SmvOpenDataDataset,
    SmvOpenDataFetch,
    parse_smv_portal_snapshot,
)
from investment_analyst.providers.peru.smv_pipeline import SmvRegistryPipeline
from investment_analyst.providers.peru.smv_raw_records import (
    SMV_COMPANIES_SOURCE_ID,
    SMV_SECURITIES_SOURCE_ID,
)
from investment_analyst.storage import LocalStorage, StoragePaths

LEGAL_NAME = "SOCIEDAD MINERA CERRO VERDE S.A.A."
COMPANY_HEADERS = (
    "Domicilio",
    "FechaInscripcion",
    "GerenteGeneral",
    "PaginaWeb",
    "PresidenteDirectorio",
    "RazonSocial",
    "ResolucionInscripcion",
    "SeccionRegistro",
    "TipoSector",
)
SECURITY_HEADERS = (
    "CodigoISIN",
    "Cotizacion",
    "DenominacionValor",
    "FechaInscripcion",
    "FechaUltCot",
    "Moneda",
    "MontoInscrito",
    "NemonicoValor",
    "RazonSocial",
    "ResolucionInscripcion",
    "TipoValor",
)
RETRIEVED_AT = datetime(2026, 7, 29, 12, tzinfo=UTC)


def _page(headers, row):
    return (
        "<html><body>"
        '<input type="hidden" name="__VIEWSTATE" value="state" />'
        f'<input id="body_txtRazonSocial" value="{LEGAL_NAME}" />'
        '<span id="body_lblEstado"></span>'
        '<table id="body_GridView1"><tr>'
        + "".join(f"<th>{header}</th>" for header in headers)
        + "</tr><tr>"
        + "".join(f"<td>{value}</td>" for value in row)
        + "</tr></table></body></html>"
    ).encode()


def _fetch(dataset, body):
    url = (
        SMV_COMPANIES_URL
        if dataset is SmvOpenDataDataset.REGISTERED_COMPANIES
        else SMV_SECURITIES_URL
    )
    snapshot = parse_smv_portal_snapshot(
        body.decode(),
        dataset=dataset,
        query_legal_name=LEGAL_NAME,
    )
    return SmvOpenDataFetch(
        snapshot=snapshot,
        requested_url=url,
        final_url=url,
        retrieved_at=RETRIEVED_AT,
        response_body=body,
        body_sha256=sha256(body).hexdigest(),
        content_type="text/html",
    )


class FixtureClient:
    def fetch_registered_company(self, legal_name):
        assert legal_name == LEGAL_NAME
        return _fetch(
            SmvOpenDataDataset.REGISTERED_COMPANIES,
            _page(
                COMPANY_HEADERS,
                (
                    "Calle Jacinto Ibañez No. 315, Arequipa",
                    "10/11/2000",
                    "GONZALES PAIHUA, TOMAS",
                    "https://www.cerroverde.pe/",
                    "STEVENS, ANTONIONI CORNELIUS",
                    LEGAL_NAME,
                    "Gerencia Mercado y Emisores 053-2000-EF/94.50",
                    "EMPRESAS EMISORAS",
                    "MINERAS",
                ),
            ),
        )

    def fetch_registered_securities(self, legal_name):
        assert legal_name == LEGAL_NAME
        return _fetch(
            SmvOpenDataDataset.REGISTERED_SECURITIES,
            _page(
                SECURITY_HEADERS,
                (
                    "64650100",
                    "69.40",
                    LEGAL_NAME,
                    "10/11/2000",
                    "09/07/2026",
                    "DOLARES",
                    "990658513.96",
                    "CVERDEC1",
                    LEGAL_NAME,
                    "Gerencia Mercado y Emisores 053-2000-EF/94.50",
                    "ACCIONES DE CAPITAL",
                ),
            ),
        )


def _seed_analytical_corpus(storage, rows):
    connection = storage.store.connection
    connection.execute(
        """
        INSERT INTO normalized_observations (
            observation_id, raw_record_id, asset_id, field_name, frequency,
            observed_at, period_end, available_at, quality, document_json
        )
        SELECT
            'foreign-observation-' || CAST(i AS VARCHAR),
            'foreign-raw-' || CAST(i AS VARCHAR),
            'equity:foreign:large-corpus',
            'foreign.value',
            'daily',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            'complete',
            '{"foreign": true}'
        FROM range(?) AS source(i)
        """,
        [rows],
    )
    connection.execute(
        """
        INSERT INTO metric_results (
            result_id, asset_id, metric_key, as_of, available_at,
            computed_at, quality, document_json
        )
        SELECT
            'foreign-metric-' || CAST(i AS VARCHAR),
            'equity:foreign:large-corpus',
            'foreign.metric',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            'complete',
            '{"foreign": true}'
        FROM range(?) AS source(i)
        """,
        [rows],
    )
    connection.execute(
        """
        INSERT INTO diagnostic_results (
            diagnostic_id, asset_id, mode, verdict, as_of, available_at,
            computed_at, quality, document_json
        )
        SELECT
            'foreign-diagnostic-' || CAST(i AS VARCHAR),
            'equity:foreign:large-corpus',
            'fundamental',
            'insufficient_data',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
            'complete',
            '{"foreign": true}'
        FROM range(?) AS source(i)
        """,
        [rows],
    )


def _current_rss_bytes():
    pages = int(Path('/proc/self/statm').read_text().split()[1])
    return pages * os.sysconf('SC_PAGE_SIZE')


def _high_water_rss_bytes():
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def main():
    workspace = Path(sys.argv[1])
    rows = int(sys.argv[2])
    tracemalloc.start()
    with LocalStorage(StoragePaths.from_root(workspace)) as storage:
        _seed_analytical_corpus(storage, rows)
        before_counts = {
            'observations': storage.observations.count(),
            'metric_results': storage.metric_results.count(),
            'diagnostics': storage.diagnostics.count(),
        }
        gc.collect()
        baseline_heap, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        baseline_rss = _current_rss_bytes()
        baseline_high_water_rss = _high_water_rss_bytes()

        first = SmvRegistryPipeline(storage, FixtureClient()).run(LEGAL_NAME)
        second = SmvRegistryPipeline(storage, FixtureClient()).run(LEGAL_NAME)

        after_counts = {
            'observations': storage.observations.count(),
            'metric_results': storage.metric_results.count(),
            'diagnostics': storage.diagnostics.count(),
        }
        current_heap, peak_heap = tracemalloc.get_traced_memory()
        peak_rss = max(_high_water_rss_bytes(), baseline_high_water_rss)
        raw_counts = {
            SMV_COMPANIES_SOURCE_ID: storage.raw_records.count(source_id=SMV_COMPANIES_SOURCE_ID),
            SMV_SECURITIES_SOURCE_ID: storage.raw_records.count(source_id=SMV_SECURITIES_SOURCE_ID),
        }

        identity = {
            'company_source_id': first.company.source_id,
            'securities_source_id': first.securities.source_id,
            'company_raw_record_id': first.company.raw_record_id,
            'securities_raw_record_id': first.securities.raw_record_id,
            'company_snapshot_sha256': first.company.semantic_sha256,
            'securities_snapshot_sha256': first.securities.semantic_sha256,
            'traceability_verified': first.traceability_verified,
        }
        result = {
            'rows_per_table': rows,
            'before_counts': before_counts,
            'after_counts': after_counts,
            'raw_counts': raw_counts,
            'first': first.to_json_dict(),
            'second': second.to_json_dict(),
            'identity': identity,
            'rss': {
                'baseline': baseline_rss,
                'peak': peak_rss,
                'final': _current_rss_bytes(),
                'peak_delta': peak_rss - baseline_high_water_rss,
            },
            'heap': {
                'baseline': baseline_heap,
                'peak': peak_heap,
                'final': current_heap,
                'peak_delta': peak_heap - baseline_heap,
            },
        }
    tracemalloc.stop()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
'''


def _run_memory_probe(tmp_path: Path, rows: int) -> dict[str, object]:
    workspace = tmp_path / f"workspace-{rows}"
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", _MEMORY_WORKER, str(workspace), str(rows)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=180,
    )
    if completed.returncode != 0:
        pytest.fail(
            "hermetic SMV memory subprocess failed\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return json.loads(completed.stdout)


def test_smv_registry_memory_is_bounded_and_duplicate_invariant(tmp_path: Path) -> None:
    small = _run_memory_probe(tmp_path, _SMALL_CORPUS_ROWS)
    large = _run_memory_probe(tmp_path, _LARGE_CORPUS_ROWS)

    for result in (small, large):
        rows = result["rows_per_table"]
        expected_counts = {
            "observations": rows,
            "metric_results": rows,
            "diagnostics": rows,
        }
        assert result["before_counts"] == expected_counts
        assert result["after_counts"] == expected_counts
        assert result["raw_counts"] == {
            "smv-open-data:registered-companies": 1,
            "smv-open-data:registered-securities": 1,
        }
        assert result["first"]["raw_records_created"] == 2
        assert result["second"]["raw_records_reused"] == 2
        assert result["identity"]["traceability_verified"] is True
        assert result["rss"]["peak_delta"] <= _PEAK_DELTA_LIMIT
        assert result["heap"]["peak_delta"] <= _PEAK_DELTA_LIMIT

    assert large["identity"] == small["identity"]
    assert large["first"] == small["first"]
    assert large["second"] == small["second"]
    assert large["rss"]["peak_delta"] <= small["rss"]["peak_delta"] + _DUPLICATE_DELTA_LIMIT
    assert large["heap"]["peak_delta"] <= small["heap"]["peak_delta"] + _DUPLICATE_DELTA_LIMIT
