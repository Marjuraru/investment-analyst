#!/usr/bin/env python3
"""Seal or read the preregistered incremental-funding study.

The command is strictly offline: it never opens the operational workspace,
never contacts a provider and never writes outside the requested output root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_EXIT_FAILURE = 1


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the preregistered incremental-funding study over one sealed historical"
            " dataset and funding bundle, or read a sealed result. The holdout stays"
            " closed and the command never opens the operational workspace."
        )
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    evaluate = subcommands.add_parser(
        "evaluate",
        help="seal or reuse one development evaluation",
        description="Seal or reuse one development evaluation; no parameter flags exist.",
    )
    evaluate.add_argument("--dataset-dir", type=_absolute, required=True)
    evaluate.add_argument("--funding-bundle-dir", type=_absolute, required=True)
    evaluate.add_argument("--output-dir", type=_absolute, required=True)
    evaluate.add_argument("--json", action="store_true", dest="as_json")
    read = subcommands.add_parser(
        "read",
        help="verify and print one sealed result",
        description="Verify one sealed artifact without importing ML dependencies.",
    )
    read.add_argument("--artifact-dir", type=_absolute, required=True)
    read.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_receipt(receipt: object) -> None:
    print(f"run_id={receipt.run_id}")
    print(f"status={receipt.status.value} report_status={receipt.report_status.value}")
    print(
        "decision={decision} holdout_status={holdout} holdout_accesses={accesses}".format(
            decision="none" if receipt.decision is None else receipt.decision.value,
            holdout=receipt.holdout_status.value,
            accesses=receipt.holdout_accesses,
        )
    )
    print(f"predictions={receipt.prediction_rows} development_fits={receipt.development_fits}")
    print(
        f"artifact={receipt.artifact_dir}"
        f" bytes={receipt.artifact_bytes}"
        f" elapsed_seconds={receipt.elapsed_seconds}"
    )
    print(f"peak_rss_bytes={receipt.peak_rss_bytes}")
    print(f"dataset_id={receipt.dataset_id} bundle_id={receipt.bundle_id}")
    print(f"protocol_sha256={receipt.protocol_sha256}")
    print(f"report_sha256={receipt.report_sha256}")
    print(f"manifest_sha256={receipt.manifest_sha256}")


def _print_report(loaded: object) -> None:
    report = loaded.report
    print(f"run_id={report.run_id}")
    print(f"status={report.status.value} decision={report.decision}")
    print(
        f"holdout={report.holdout.status.value} accesses={report.holdout.accesses}"
        f" predictions={report.prediction_rows} development_fits={report.development_fits}"
    )
    print(f"protocol_sha256={report.protocol_sha256}")
    print(f"dataset_id={report.dataset_id} bundle_id={report.bundle_id}")
    if report.pooled_comparisons:
        for comparison in report.pooled_comparisons:
            print(
                f"comparison={comparison.reference.value}"
                f" brier={comparison.model_brier}/{comparison.reference_brier}"
                f" lower_bound={comparison.lower_bound}"
                f" criterion={comparison.criterion_satisfied}"
            )


def _evaluate(args: argparse.Namespace) -> int:
    from investment_analyst.research.ml_adapter import MlAdapterError
    from investment_analyst.research.ml_funding_export import (
        FundingMlExportError,
        evaluate_funding_experiment,
    )

    try:
        receipt = evaluate_funding_experiment(
            dataset_dir=args.dataset_dir,
            bundle_dir=args.funding_bundle_dir,
            output_dir=args.output_dir,
        )
    except (MlAdapterError, FundingMlExportError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    if args.as_json:
        print(json.dumps(receipt.to_json_dict(), ensure_ascii=False, indent=2))
    else:
        _print_receipt(receipt)
    return 0


def _read(args: argparse.Namespace) -> int:
    from investment_analyst.research.ml_funding_export import (
        FundingMlExportError,
        read_funding_ml_artifact,
    )

    try:
        loaded = read_funding_ml_artifact(args.artifact_dir)
    except (FundingMlExportError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    if args.as_json:
        print(
            json.dumps(
                loaded.report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_report(loaded)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "evaluate":
        return _evaluate(args)
    return _read(args)


if __name__ == "__main__":
    raise SystemExit(main())
