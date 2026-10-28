#!/usr/bin/env python3
"""Read-only ADR valuation smoke over one explicit active v2 workspace.

Queries only the corporate valuation and universe coverage projections for four
catalog assets. It never acquires providers, opens a writer, activates a release
or mutates the workspace.
"""

import argparse
import json
import platform
import resource
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

import investment_analyst
from investment_analyst.analytics.valuation.models import CorporateValuationRequest
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage_models import (
    UniverseCoverageRequest,
)
from investment_analyst.storage import StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_SCHEMA = "adr-valuation-smoke-v1"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_ASSETS = ("equity:us:aapl", "equity:us:amd", "equity:us:bvn", "equity:us:tsm")
_OPEN_TIMEOUT_SECONDS = 180.0
_QUERY_TIMEOUT_SECONDS = 120.0
_MEMORY_LIMIT_BYTES = 3 * 1024**3


class _Measurement(TypedDict):
    milliseconds: float
    outcome: str


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _measure(callable_query: Callable[[], object]) -> tuple[object, _Measurement]:
    started = time.perf_counter()
    try:
        result = callable_query()
    except Exception as error:  # the receipt records the failure verbatim
        return None, {
            "milliseconds": round((time.perf_counter() - started) * 1000, 3),
            "outcome": f"error:{type(error).__name__}:{error}",
        }
    return result, {
        "milliseconds": round((time.perf_counter() - started) * 1000, 3),
        "outcome": "ok",
    }


def _basis_receipt(basis: object) -> dict[str, object] | None:
    if basis is None:
        return None
    evidence = getattr(basis, "basis_evidence", None)
    receipt: dict[str, object] = {
        "basis": basis.basis,
        "contract_version": basis.contract_version,
        "market_adjustment": basis.market_adjustment,
        "market_units_per_reported_share": str(basis.market_units_per_reported_share),
    }
    if evidence is not None:
        receipt["evidence"] = {
            "accession_number": evidence.accession_number,
            "available_at": evidence.available_at,
            "cik": evidence.cik,
            "factor": str(evidence.factor),
            "reported_share_unit": evidence.reported_share_unit,
            "revision": evidence.revision,
            "source_sha256": evidence.source_sha256,
            "source_url": evidence.source_url,
            "valid_from": evidence.valid_from,
            "valid_to": evidence.valid_to,
        }
    return receipt


def _valuation_receipt(
    application: InvestmentAnalystApplication,
    location: StorageLocationRequest,
    *,
    asset_id: str,
    known_at: datetime,
    valuation_date: date,
) -> dict[str, object]:
    def query() -> object:
        return application.query_corporate_valuation(
            CorporateValuationRequest(
                asset_id=asset_id,
                known_at=known_at,
                valuation_date=valuation_date,
            ),
            location=location,
        )

    first, first_measurement = _measure(query)
    _, second_measurement = _measure(query)
    if first is None:
        return {
            "asset_id": asset_id,
            "error": first_measurement["outcome"],
            "measurements": [first_measurement, second_measurement],
        }
    snapshot = first
    return {
        "asset_id": asset_id,
        "available_at": snapshot.available_at,
        "basis": _basis_receipt(snapshot.security_basis),
        "coverage": snapshot.coverage.model_dump(mode="json"),
        "filing_accession_number": snapshot.filing_accession_number,
        "known_at": snapshot.known_at,
        "measurements": [first_measurement, second_measurement],
        "metrics": [
            {
                "available_at": metric.available_at,
                "input_count": len(metric.input_observation_ids),
                "metric_key": metric.metric_key,
                "reason_code": (
                    metric.reason_code.value if metric.reason_code is not None else None
                ),
                "status": metric.status.value,
                "value": metric.value,
            }
            for metric in snapshot.metrics
        ],
        "price_source_id": snapshot.price_source_id,
        "status": snapshot.status.value,
        "valuation_as_of": snapshot.valuation_as_of,
    }


def _coverage_receipt(
    application: InvestmentAnalystApplication,
    location: StorageLocationRequest,
    *,
    known_at: datetime,
    valuation_date: date,
) -> dict[str, object]:
    request = UniverseCoverageRequest(
        known_at=known_at,
        market_start=valuation_date - timedelta(days=365),
        market_end=valuation_date,
        fundamental_start=valuation_date - timedelta(days=3660),
        fundamental_end=valuation_date,
        asset_ids=_ASSETS,
    )

    def query() -> object:
        return application.query_universe_coverage(request, location=location)

    first, first_measurement = _measure(query)
    _, second_measurement = _measure(query)
    if first is None:
        return {
            "error": first_measurement["outcome"],
            "measurements": [first_measurement, second_measurement],
        }
    result = first
    return {
        "assets": [
            {
                "asset_id": asset.asset_id,
                "corporate_valuation": {
                    "capability": asset.corporate_valuation.capability.value,
                    "evidence": asset.corporate_valuation.evidence.value,
                    "latest_input_available_at": (
                        asset.corporate_valuation.latest_input_available_at
                    ),
                    "reason_codes": list(asset.corporate_valuation.reason_codes),
                    "status": asset.corporate_valuation.status,
                    "valuation_as_of": asset.corporate_valuation.valuation_as_of,
                },
                "limitations": list(asset.limitations),
            }
            for asset in result.assets
        ],
        "catalog_sha256": result.catalog_sha256,
        "measurements": [first_measurement, second_measurement],
    }


def _workspace_identity(root: Path, expected_workspace_id: str | None) -> dict[str, object]:
    started = time.perf_counter()
    inspection = WorkspaceService().inspect(root)
    opening_ms = round((time.perf_counter() - started) * 1000, 3)
    if opening_ms > _OPEN_TIMEOUT_SECONDS * 1000:
        raise SystemExit("workspace inspection exceeded the admitted opening timeout")
    if inspection.format_version != 2:
        raise SystemExit("workspace format is not an active v2 workspace")
    if inspection.status != "ready":
        raise SystemExit("workspace inspection is not ready")
    if expected_workspace_id is not None and str(inspection.workspace_id) != (
        expected_workspace_id
    ):
        raise SystemExit("workspace identity does not match the expected workspace id")
    storage_paths = StoragePaths.from_workspace_root(
        inspection.workspace_root,
        format_version=inspection.format_version,
        workspace_id=inspection.workspace_id,
    )
    database_path = storage_paths.database_path
    return {
        "database_bytes": database_path.stat().st_size if database_path.is_file() else 0,
        "format_version": inspection.format_version,
        "inspection_ms": opening_ms,
        "metric_result_count": inspection.metric_result_count,
        "observation_count": inspection.observation_count,
        "status": inspection.status,
        "workspace_id": str(inspection.workspace_id),
        "workspace_root": str(inspection.workspace_root),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--known-at", required=True)
    parser.add_argument("--valuation-date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-workspace-id")
    return parser


def _receipt_measurements(receipt: dict[str, object]) -> list[dict[str, object]]:
    measurements = receipt.get("measurements", [])
    return list(measurements) if isinstance(measurements, list) else []


def run(
    *,
    workspace: Path,
    known_at: datetime,
    valuation_date: date,
    output: Path,
    expected_workspace_id: str | None,
) -> dict[str, object]:
    root = workspace.expanduser().resolve()
    identity = _workspace_identity(root, expected_workspace_id)
    application = InvestmentAnalystApplication.create_default()
    location = StorageLocationRequest(workspace=root)
    started = time.perf_counter()
    valuations = {
        asset_id: _valuation_receipt(
            application,
            location,
            asset_id=asset_id,
            known_at=known_at,
            valuation_date=valuation_date,
        )
        for asset_id in _ASSETS
    }
    coverage = _coverage_receipt(
        application,
        location,
        known_at=known_at,
        valuation_date=valuation_date,
    )
    elapsed = time.perf_counter() - started
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    children_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    durations = [
        float(measurement["milliseconds"])
        for receipt in (*valuations.values(), coverage)
        for measurement in _receipt_measurements(receipt)
    ]
    max_duration = max(durations, default=0.0)
    if max_duration > _QUERY_TIMEOUT_SECONDS * 1000:
        return {
            "schema_version": _SCHEMA,
            "admission": "rejected",
            "reason": "a read exceeded the per-query timeout",
            "max_query_ms": max_duration,
        }
    if self_rss + children_rss > _MEMORY_LIMIT_BYTES:
        return {
            "schema_version": _SCHEMA,
            "admission": "rejected",
            "reason": "process peak memory exceeded the admitted bound",
            "max_rss_bytes": self_rss + children_rss,
        }
    receipt: dict[str, object] = {
        "schema_version": _SCHEMA,
        "admission": "admitted",
        "code": {
            "dirty": bool(_git(_REPOSITORY_ROOT, "status", "--short")),
            "package_path": str(Path(investment_analyst.__file__).resolve()),
            "sha": _git(_REPOSITORY_ROOT, "rev-parse", "HEAD"),
            "tree": _git(_REPOSITORY_ROOT, "rev-parse", "HEAD^{tree}"),
        },
        "environment": {
            "implementation": sys.implementation.name,
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "request": {
            "assets": list(_ASSETS),
            "known_at": known_at,
            "valuation_date": valuation_date,
        },
        "workspace": identity,
        "valuations": valuations,
        "coverage": coverage,
        "measurements": {
            "bytes_read": "UNKNOWN",
            "elapsed_seconds": round(elapsed, 3),
            "max_query_ms": max_duration,
            "max_rss_bytes": self_rss + children_rss,
            "rows_hydrated": "UNKNOWN",
        },
        "limitations": [
            "IEX daily bars are one-exchange coverage, not consolidated SIP.",
            "Annual IFRS facts only; no FX, TTM, quarterly or corporate-action watch.",
            "The smoke reads the active workspace read-only and proves no deployment.",
        ],
    }
    serialized = json.dumps(receipt, default=_json_value, allow_nan=False, indent=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{serialized}\n", encoding="utf-8")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    known_at = datetime.fromisoformat(arguments.known_at.replace("Z", "+00:00"))
    if known_at.tzinfo is None:
        raise SystemExit("--known-at must include a timezone")
    valuation_date = date.fromisoformat(arguments.valuation_date)
    started = time.perf_counter()
    receipt = run(
        workspace=arguments.workspace,
        known_at=known_at.astimezone(UTC),
        valuation_date=valuation_date,
        output=arguments.output,
        expected_workspace_id=arguments.expected_workspace_id,
    )
    print(
        json.dumps(
            {
                "admission": receipt.get("admission"),
                "assets": len(_ASSETS),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "output": str(arguments.output),
                "schema_version": receipt.get("schema_version"),
            },
            sort_keys=True,
        )
    )
    return 0 if receipt.get("admission") == "admitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
