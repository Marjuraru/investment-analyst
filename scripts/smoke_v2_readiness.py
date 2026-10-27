#!/usr/bin/env python3
"""Read-only v2 readiness smoke: latency matrix, reopen and absence causes."""

import argparse
import hashlib
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

_SCHEMA = "v2-readiness-smoke-v1"
_ASSETS = ("equity:us:aapl", "equity:us:amd", "equity:us:tsm", "equity:us:bvn")
_BENCHMARK = "equity:us:aapl"
_COMPARISON_ASSETS = ("equity:us:aapl", "equity:us:amd", "equity:us:tsm", "crypto:btc-usd")


@dataclass(frozen=True, slots=True)
class _Measurement:
    name: str
    method: str
    path: str
    query: tuple[tuple[str, str], ...]

    def url(self, base_url: str) -> str:
        encoded = urlencode(self.query)
        return (
            f"{base_url.rstrip('/')}{self.path}?{encoded}"
            if encoded
            else (f"{base_url.rstrip('/')}{self.path}")
        )


def _windows(known_at: datetime) -> dict[str, date]:
    end = known_at.astimezone(UTC).date() - timedelta(days=1)
    return {
        "market_start": end - timedelta(days=365),
        "market_end": end,
        "fundamental_start": end - timedelta(days=3660),
        "fundamental_end": end,
    }


def _coverage_query(
    known_at: datetime,
    windows: dict[str, date],
    *,
    asset_ids: tuple[str, ...] = (),
    frequency: str = "annual",
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = [
        ("known_at", known_at.astimezone(UTC).isoformat()),
        ("market_start", windows["market_start"].isoformat()),
        ("market_end", windows["market_end"].isoformat()),
        ("fundamental_start", windows["fundamental_start"].isoformat()),
        ("fundamental_end", windows["fundamental_end"].isoformat()),
        ("frequency", frequency),
    ]
    query.extend(("asset_id", asset_id) for asset_id in asset_ids)
    return tuple(query)


def _scenarios(
    known_at: datetime,
    windows: dict[str, date],
    frequencies: dict[str, str],
) -> tuple[_Measurement, ...]:
    scenarios = [
        _Measurement("shell", "GET", "/", ()),
        _Measurement("overview", "GET", "/api/v1/overview", ()),
        _Measurement(
            "matrix37",
            "GET",
            "/api/v1/universe-coverage",
            _coverage_query(known_at, windows),
        ),
        _Measurement(
            "reopen_matrix37",
            "GET",
            "/api/v1/universe-coverage",
            _coverage_query(known_at, windows),
        ),
    ]
    for asset_id in _ASSETS:
        scenarios.append(
            _Measurement(
                f"fundamentals:{asset_id}",
                "GET",
                "/api/fundamental-analysis",
                (
                    ("asset_id", asset_id),
                    ("known_at", known_at.astimezone(UTC).isoformat()),
                    ("frequency", frequencies[asset_id]),
                ),
            )
        )
        scenarios.append(
            _Measurement(
                f"valuation:{asset_id}",
                "GET",
                "/api/v1/valuation",
                (
                    ("asset_id", asset_id),
                    ("known_at", known_at.astimezone(UTC).isoformat()),
                    ("valuation_date", windows["market_end"].isoformat()),
                    ("basis", "latest_annual"),
                ),
            )
        )
    scenarios.append(
        _Measurement(
            "market_chart:equity:us:aapl",
            "GET",
            "/api/market-chart",
            (
                ("asset_id", "equity:us:aapl"),
                ("known_at", known_at.astimezone(UTC).isoformat()),
                ("period", "6m"),
            ),
        )
    )
    scenarios.append(
        _Measurement(
            "market_comparison",
            "GET",
            "/api/v1/market-comparison",
            (
                ("benchmark_id", _BENCHMARK),
                ("start", (windows["market_end"] - timedelta(days=90)).isoformat()),
                ("end", windows["market_end"].isoformat()),
                ("known_at", known_at.astimezone(UTC).isoformat()),
            )
            + tuple(("asset_id", asset_id) for asset_id in _COMPARISON_ASSETS),
        )
    )
    return tuple(scenarios)


def _fetch(url: str, timeout: float = 60.0) -> tuple[int, bytes, float, str | None]:
    started = time.perf_counter_ns()
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - verified loopback URL
            payload = response.read()
            status = response.status
    except HTTPError as error:
        return error.code, error.read(), (time.perf_counter_ns() - started) / 1_000_000, str(error)
    except (OSError, URLError) as error:
        return 0, b"", (time.perf_counter_ns() - started) / 1_000_000, str(error)
    return status, payload, (time.perf_counter_ns() - started) / 1_000_000, None


def _measure(
    measurement: _Measurement,
    base_url: str,
    samples: int,
    timeout: float,
) -> dict[str, object]:
    durations: list[float] = []
    statuses: list[int] = []
    errors: list[str] = []
    payload_bytes = 0
    digest = ""
    for _ in range(samples):
        status, payload, elapsed_ms, error = _fetch(measurement.url(base_url), timeout)
        durations.append(elapsed_ms)
        statuses.append(status)
        payload_bytes = len(payload)
        if error is not None:
            errors.append(error)
        if payload:
            digest = hashlib.sha256(payload).hexdigest()
    ordered = sorted(durations)
    return {
        "name": measurement.name,
        "method": measurement.method,
        "path": measurement.path,
        "samples": samples,
        "statuses": statuses,
        "errors": errors,
        "min_ms": round(ordered[0], 3),
        "p50_ms": round(statistics.median(ordered), 3),
        "max_ms": round(ordered[-1], 3),
        "payload_bytes": payload_bytes,
        "payload_sha256": digest,
    }


def _json(path: str, base_url: str, timeout: float) -> dict[str, Any]:
    status, payload, _, error = _fetch(f"{base_url.rstrip('/')}{path}", timeout)
    if status != 200 or error is not None:
        raise RuntimeError(f"{path} returned status {status}: {error or 'no body'}")
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return document


def _coverage_for(
    base_url: str,
    known_at: datetime,
    windows: dict[str, date],
    *,
    asset_id: str,
    frequency: str,
    timeout: float,
) -> dict[str, Any]:
    query = _coverage_query(known_at, windows, asset_ids=(asset_id,), frequency=frequency)
    return _json(
        f"/api/v1/universe-coverage?{urlencode(dict(query))}",
        base_url,
        timeout,
    )


def _fundamentals_for(
    base_url: str,
    known_at: datetime,
    *,
    asset_id: str,
    frequency: str,
    timeout: float,
) -> dict[str, Any]:
    query = urlencode(
        {
            "asset_id": asset_id,
            "known_at": known_at.astimezone(UTC).isoformat(),
            "frequency": frequency,
        }
    )
    return _json(f"/api/fundamental-analysis?{query}", base_url, timeout)


def _valuation_for(
    base_url: str,
    known_at: datetime,
    windows: dict[str, date],
    *,
    asset_id: str,
    timeout: float,
) -> dict[str, Any]:
    query = urlencode(
        {
            "asset_id": asset_id,
            "known_at": known_at.astimezone(UTC).isoformat(),
            "valuation_date": windows["market_end"].isoformat(),
            "basis": "latest_annual",
        }
    )
    return _json(f"/api/v1/valuation?{query}", base_url, timeout)


def _preferences(base_url: str, timeout: float) -> dict[str, Any]:
    return _json("/api/v1/asset-preferences", base_url, timeout)


def _scheduled(preferences: dict[str, Any], asset_id: str) -> bool | None:
    rows = preferences.get("assets") or preferences.get("preferences") or ()
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("asset_id") == asset_id:
            value = row.get("effective_scheduled_refresh")
            return value if isinstance(value, bool) else None
    return None


def _cause_matrix(
    base_url: str,
    known_at: datetime,
    windows: dict[str, date],
    frequencies: dict[str, str],
    timeout: float,
) -> dict[str, object]:
    preferences = _preferences(base_url, timeout)
    assets: list[dict[str, object]] = []
    for asset_id in _ASSETS:
        frequency = frequencies[asset_id]
        coverage = _coverage_for(
            base_url,
            known_at,
            windows,
            asset_id=asset_id,
            frequency=frequency,
            timeout=timeout,
        )
        quarterly = _coverage_for(
            base_url,
            known_at,
            windows,
            asset_id=asset_id,
            frequency="quarterly",
            timeout=timeout,
        )
        fundamentals = _fundamentals_for(
            base_url,
            known_at,
            asset_id=asset_id,
            frequency=frequency,
            timeout=timeout,
        )
        valuation = _valuation_for(
            base_url,
            known_at,
            windows,
            asset_id=asset_id,
            timeout=timeout,
        )
        coverage_asset = (coverage.get("assets") or [{}])[0]
        quarterly_asset = (quarterly.get("assets") or [{}])[0]
        history = fundamentals.get("history") or {}
        research = history.get("research") or {}
        research_coverage = research.get("coverage") or {}
        reasons = sorted(
            {
                metric.get("reason_code")
                for metric in valuation.get("metrics", ())
                if isinstance(metric, dict) and metric.get("reason_code")
            }
        )
        reasons = [str(reason) for reason in reasons]
        skipped = research_coverage.get("skipped_counts") or {}
        causes = _causes(
            coverage_asset,
            quarterly_asset,
            research_coverage,
            reasons,
            scheduled=_scheduled(preferences, asset_id),
        )
        assets.append(
            {
                "asset_id": asset_id,
                "scheduled_refresh": _scheduled(preferences, asset_id),
                "frequency": frequency,
                "coverage": {
                    "fundamentals": coverage_asset.get("fundamentals"),
                    "corporate_valuation": coverage_asset.get("corporate_valuation"),
                    "limitations": coverage_asset.get("limitations"),
                },
                "quarterly_coverage": quarterly_asset.get("fundamentals"),
                "fundamentals": {
                    "frequency": research.get("request", {}).get("frequency"),
                    "source_periods": research_coverage.get("source_periods"),
                    "output_periods": research_coverage.get("output_periods"),
                    "metrics_returned": research_coverage.get("metrics_returned"),
                    "skipped_counts": skipped,
                    "missing_requirements": (fundamentals.get("classification") or {}).get(
                        "missing_requirements"
                    ),
                    "classification_status": (fundamentals.get("classification") or {}).get(
                        "status"
                    ),
                },
                "valuation": {
                    "status": valuation.get("status"),
                    "coverage": valuation.get("coverage"),
                    "reason_codes": reasons,
                    "valuation_as_of": valuation.get("valuation_as_of"),
                    "security_basis": valuation.get("security_basis"),
                },
                "causes": causes,
            }
        )
    return {
        "scheduler": {
            "scheduled_asset_count": preferences.get("scheduled_asset_count"),
            "scheduler_enabled": preferences.get("scheduler_enabled"),
        },
        "assets": assets,
    }


def _causes(
    coverage: dict[str, Any],
    quarterly: dict[str, Any],
    research_coverage: dict[str, Any],
    reasons: list[str],
    *,
    scheduled: bool | None,
) -> list[str]:
    causes: list[str] = []
    fundamentals = coverage.get("fundamentals") or {}
    quarterly_fundamentals = quarterly.get("fundamentals") or {}
    if quarterly_fundamentals.get("capability") == "not_implemented":
        causes.append("fundamental_frequency_not_implemented")
    if "share_basis_unavailable" in reasons:
        causes.append("share_basis_unavailable")
    if fundamentals.get("evidence") == "missing":
        causes.append("not_scheduled_or_missing_at_cut" if scheduled is False else "missing_at_cut")
    skipped = research_coverage.get("skipped_counts") or {}
    if any(str(key).endswith(":missing_inputs") for key in skipped):
        causes.append("official_input_not_available_or_not_represented")
    if any(str(key).endswith(":invalid_denominator") for key in skipped):
        causes.append("non_positive_or_invalid_denominator")
    if not causes:
        causes.append("present")
    return causes


def _read_performance(base_url: str, timeout: float) -> dict[str, Any]:
    return _json("/api/v1/read-performance", base_url, timeout)


def _frequencies(base_url: str, timeout: float) -> dict[str, str]:
    document = _json("/api/market-assets", base_url, timeout)
    assets = document.get("assets") or ()
    frequencies: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("asset_id") not in _ASSETS:
            continue
        supported = asset.get("fundamental_frequencies") or ()
        if "quarterly" in supported:
            frequencies[str(asset["asset_id"])] = "quarterly"
        elif "annual" in supported:
            frequencies[str(asset["asset_id"])] = "annual"
        else:
            frequencies[str(asset["asset_id"])] = "annual"
    for asset_id in _ASSETS:
        frequencies.setdefault(asset_id, "annual")
    return frequencies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--known-at", required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--browser-json", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()
    if not 1 <= arguments.samples <= 50:
        parser.error("samples must be between 1 and 50")
    try:
        known_at = datetime.fromisoformat(arguments.known_at.replace("Z", "+00:00"))
    except ValueError:
        parser.error("known_at must use ISO-8601")
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        parser.error("known_at must include a UTC offset")
    known_at = known_at.astimezone(UTC)
    windows = _windows(known_at)
    started = datetime.now(UTC)
    try:
        frequencies = _frequencies(arguments.base_url, arguments.timeout_seconds)
        before = _read_performance(arguments.base_url, arguments.timeout_seconds)
        measurements = tuple(
            _measure(
                scenario,
                arguments.base_url,
                arguments.samples,
                arguments.timeout_seconds,
            )
            for scenario in _scenarios(known_at, windows, frequencies)
        )
        after = _read_performance(arguments.base_url, arguments.timeout_seconds)
        causes = _cause_matrix(
            arguments.base_url,
            known_at,
            windows,
            frequencies,
            arguments.timeout_seconds,
        )
    except (RuntimeError, ValueError, HTTPError, URLError) as error:
        print(f"v2 readiness smoke failed: {error}", file=sys.stderr)
        return 2
    browser = None
    if arguments.browser_json is not None:
        try:
            browser = json.loads(arguments.browser_json.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"browser evidence could not be read: {error}", file=sys.stderr)
            return 2
    errors = sum(len(item["errors"]) for item in measurements)
    statuses = {status for item in measurements for status in item["statuses"]}
    matrix = next(item for item in measurements if item["name"] == "matrix37")
    reopen = next(item for item in measurements if item["name"] == "reopen_matrix37")
    shell = next(item for item in measurements if item["name"] == "shell")
    document = {
        "schema_version": _SCHEMA,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "known_at": known_at.isoformat(),
        "base_url": arguments.base_url,
        "windows": {key: value.isoformat() for key, value in windows.items()},
        "frequencies": frequencies,
        "read_performance_before": before,
        "read_performance_after": after,
        "measurements": list(measurements),
        "cause_matrix": causes,
        "browser": browser,
        "summary": {
            "samples_per_scenario": arguments.samples,
            "errors": errors,
            "statuses": sorted(statuses),
            "shell_p50_ms": shell["p50_ms"],
            "shell_max_ms": shell["max_ms"],
            "matrix_p50_ms": matrix["p50_ms"],
            "matrix_max_ms": matrix["max_ms"],
            "reopen_p50_ms": reopen["p50_ms"],
            "reopen_max_ms": reopen["max_ms"],
            "server_5xx": any(status >= 500 for status in statuses),
        },
    }
    serialized = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if errors == 0 and not document["summary"]["server_5xx"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
