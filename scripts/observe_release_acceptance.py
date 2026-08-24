#!/usr/bin/env python3
"""Run one bounded, read-only exact-SHA release acceptance observation."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from investment_analyst.application.release_acceptance import (
    MAX_DURATION_SECONDS,
    MAX_INTERVAL_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_INTERVAL_SECONDS,
    MIN_TIMEOUT_SECONDS,
    SERVICE_NAME,
    ReleaseAcceptanceConfig,
    ReleaseAcceptanceError,
    observe_release_acceptance,
)


def _bounded_float(
    value: str,
    *,
    minimum: float,
    maximum: float,
    allow_zero: bool = False,
    label: str,
) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be numeric") from error
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise argparse.ArgumentTypeError(f"{label} is outside its allowed bounds")
    if not allow_zero and parsed == 0:
        raise argparse.ArgumentTypeError(f"{label} must be positive")
    return parsed


def _duration(value: str) -> float:
    return _bounded_float(
        value,
        minimum=0.0,
        maximum=MAX_DURATION_SECONDS,
        allow_zero=True,
        label="duration",
    )


def _interval(value: str) -> float:
    return _bounded_float(
        value,
        minimum=MIN_INTERVAL_SECONDS,
        maximum=MAX_INTERVAL_SECONDS,
        label="interval",
    )


def _timeout(value: str) -> float:
    return _bounded_float(
        value,
        minimum=MIN_TIMEOUT_SECONDS,
        maximum=MAX_TIMEOUT_SECONDS,
        label="timeout",
    )


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port is outside its allowed bounds")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observe_release_acceptance",
        description="Observe one exact-SHA local release using read-only local probes.",
    )
    parser.add_argument("--sha", required=True, help="Full candidate commit SHA")
    parser.add_argument("--tree", required=True, help="Full candidate tree SHA")
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--jsonl", required=True, type=Path, help="New append-only JSONL path")
    parser.add_argument("--summary", required=True, type=Path, help="New atomic summary path")
    parser.add_argument("--service", default=SERVICE_NAME)
    parser.add_argument("--port", default=8765, type=_port)
    parser.add_argument("--duration-seconds", required=True, type=_duration)
    parser.add_argument("--interval-seconds", required=True, type=_interval)
    parser.add_argument("--timeout-seconds", default=5.0, type=_timeout)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = observe_release_acceptance(
            ReleaseAcceptanceConfig(
                expected_sha=args.sha,
                expected_tree=args.tree,
                service=args.service,
                workspace_root=args.workspace_root,
                jsonl_path=args.jsonl,
                summary_path=args.summary,
                duration_seconds=args.duration_seconds,
                interval_seconds=args.interval_seconds,
                timeout_seconds=args.timeout_seconds,
                port=args.port,
            )
        )
    except ReleaseAcceptanceError as error:
        print(f"observe_release_acceptance error: {error.code}", file=sys.stderr)
        return 2
    except Exception:
        print("observe_release_acceptance error: observer_failed", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "schema_version": summary["schema_version"],
                "status": summary["status"],
                "sample_count": summary["sample_count"],
                "failures": summary["failures"],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0 if summary["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
