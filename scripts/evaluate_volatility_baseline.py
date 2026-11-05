#!/usr/bin/env python3
"""Seal or reuse one preregistered volatility study over a sealed archive.

The command is strictly offline: it never opens the operational workspace,
never contacts a provider and never writes outside the requested output root.
The help path deliberately imports no numerical dependency.
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
            "Evaluate the preregistered five-day log-variation study over one sealed"
            " Coinbase daily archive. The reserved period is never evaluated, the"
            " holdout is never opened and no training parameter is configurable."
        )
    )
    parser.add_argument("--archive-dir", type=_absolute, required=True)
    parser.add_argument("--output-dir", type=_absolute, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_receipt(receipt: object, report: object) -> None:
    print(f"run_id={receipt.run_id}")
    print(f"status={receipt.status.value} report_status={receipt.report_status.value}")
    print(f"decision={receipt.decision.value if receipt.decision is not None else 'none'}")
    print(f"archive_id={receipt.archive_id} semantic_sha256={receipt.archive_semantic_sha256}")
    counts = report.counts
    print(
        f"decisions_planned={counts.planned_decisions}"
        f" constructed={counts.constructed_decisions}"
        f" reserved={counts.reserved_decisions_not_constructed}"
        f" feature_ready={counts.feature_ready}"
        f" target_ready={counts.target_ready}"
        f" candles={counts.candles_read}"
    )
    print(
        f"fits={report.development_fits}"
        f" fits_this_invocation={receipt.fits_executed_this_invocation}"
        f" resumed={receipt.resumed_from_progress}"
        f" holdout={report.holdout_status}"
        f" predictions={report.prediction_rows}"
    )
    for metric in report.pooled_scores:
        print(
            f"pooled model={metric.model.value} rows={metric.rows}"
            f" mse={metric.mse} mae={metric.mae}"
        )
    for comparison in report.pooled_comparisons:
        print(
            f"comparison reference={comparison.reference.value}"
            f" relative={comparison.relative_improvement}"
            f" lower_bound={comparison.lower_bound}"
            f" positive={comparison.lower_bound_positive}"
        )
    if report.decision_reason is not None:
        print(f"reason={report.decision_reason}")
    print(
        f"elapsed_seconds={receipt.elapsed_seconds}"
        f" peak_rss_bytes={receipt.peak_rss_bytes}"
        f" artifact_bytes={receipt.artifact_bytes}"
        f" root_bytes={receipt.root_bytes}"
    )
    print(
        f"protocol_sha256={receipt.protocol_sha256}"
        f" report_sha256={receipt.report_sha256}"
        f" predictions_sha256={receipt.predictions_sha256}"
        f" manifest_sha256={receipt.manifest_sha256}"
    )
    superseded = ",".join(str(value) for value in receipt.superseded_run_ids) or "none"
    print(f"retention final={receipt.final_run_id} superseded={superseded}")


def _payload(receipt: object, report: object) -> dict[str, object]:
    payload = receipt.to_json_dict()
    payload["counts"] = report.counts.model_dump(mode="json")
    payload["pooled_scores"] = [metric.model_dump(mode="json") for metric in report.pooled_scores]
    payload["pooled_comparisons"] = [
        comparison.model_dump(mode="json") for comparison in report.pooled_comparisons
    ]
    payload["decision_reason"] = report.decision_reason
    return payload


def _evaluate(arguments: argparse.Namespace) -> int:
    from investment_analyst.research.volatility_export import (
        VolatilityExportError,
        evaluate_volatility_experiment,
        read_volatility_artifact,
    )

    try:
        receipt = evaluate_volatility_experiment(
            archive_dir=arguments.archive_dir,
            output_dir=arguments.output_dir,
        )
        loaded = read_volatility_artifact(Path(receipt.artifact_dir))
    except (VolatilityExportError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    if arguments.as_json:
        print(
            json.dumps(
                _payload(receipt, loaded.report),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        _print_receipt(receipt, loaded.report)
    if receipt.report_status.value == "error":
        return _EXIT_FAILURE
    return 0


def main(argv: list[str] | None = None) -> int:
    return _evaluate(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
