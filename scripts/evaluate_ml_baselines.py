#!/usr/bin/env python3
"""Evaluate the preregistered ML baselines over one sealed historical dataset.

The command is strictly offline: it never opens the operational workspace,
never contacts a provider and never writes outside the requested output root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_EXIT_INVALID_ARGS = 2
_EXIT_FAILURE = 1


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the preregistered ML baseline study over a sealed historical research"
            " dataset and seal a verifiable artifact. The evaluation is offline and does"
            " not open the operational workspace."
        )
    )
    parser.add_argument("--dataset-dir", type=_absolute, required=True)
    parser.add_argument("--output-dir", type=_absolute, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_human(receipt: object) -> None:
    print(f"run_id={receipt.run_id}")
    print(f"status={receipt.status.value} report_status={receipt.report_status.value}")
    print(
        "decision={decision} holdout_status={holdout}".format(
            decision="none" if receipt.decision is None else receipt.decision.value,
            holdout=receipt.holdout_status.value,
        )
    )
    print(
        f"predictions={receipt.prediction_rows}"
        f" development_fits={receipt.development_fits}"
        f" holdout_fits={receipt.holdout_fits}"
        f" holdout_accesses={receipt.holdout_accesses}"
    )
    print(
        f"artifact={receipt.artifact_dir}"
        f" bytes={receipt.artifact_bytes}"
        f" elapsed_seconds={receipt.elapsed_seconds}"
    )
    print(f"peak_rss_bytes={receipt.peak_rss_bytes}")
    print(f"dataset_id={receipt.dataset_id}")
    print(f"dataset_content_sha256={receipt.dataset_content_sha256}")
    print(f"protocol_sha256={receipt.protocol_sha256}")
    print(f"report_sha256={receipt.report_sha256}")
    print(f"manifest_sha256={receipt.manifest_sha256}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from investment_analyst.research.ml_adapter import MlAdapterError
        from investment_analyst.research.ml_evaluation import MlEvaluationError
        from investment_analyst.research.ml_export import MlExportError, evaluate_dataset
    except ImportError as error:  # pragma: no cover - exercised through subprocess smoke
        print(f"error: ML dependencies are unavailable: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    try:
        receipt = evaluate_dataset(dataset_dir=args.dataset_dir, output_dir=args.output_dir)
    except (MlAdapterError, MlEvaluationError, MlExportError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    if args.as_json:
        print(json.dumps(receipt.to_json_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
