#!/usr/bin/env python3
"""Acquire a sealed Deribit funding archive or build its research complement."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.providers.crypto.deribit import DeribitClient, DeribitError
from investment_analyst.providers.http import HttpRequestError, UrlLibHttpTransport
from investment_analyst.research.funding_archive import (
    FundingArchiveError,
    FundingArchiveReceipt,
    acquire_funding_archive,
    read_funding_archive,
)
from investment_analyst.research.funding_export import (
    FundingBundleReceipt,
    FundingExportError,
    export_funding_bundle,
    load_base_dataset,
)
from investment_analyst.research.funding_features import (
    FundingFeatureError,
    build_funding_bundle,
    funding_bundle_view_summary,
)
from investment_analyst.research.funding_models import (
    FundingBundleSummary,
    funding_archive_request,
)

_EXIT_INVALID = 2


def _known_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--known-at must include timezone information")
    return parsed.astimezone(UTC)


def _absolute_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire a finite Deribit funding archive or rebuild the causal funding"
            " complement offline. This command never opens the operational workspace."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire = subparsers.add_parser(
        "acquire",
        help="Fetch and seal one bounded Deribit funding archive.",
    )
    acquire.add_argument("--dataset-dir", type=_absolute_dir, required=True)
    acquire.add_argument("--asset-id", required=True)
    acquire.add_argument("--output-dir", type=_absolute_dir, required=True)
    acquire.add_argument("--json", action="store_true", dest="as_json")

    build = subparsers.add_parser(
        "build",
        help="Build the funding complement from a sealed archive, optionally exporting it.",
    )
    build.add_argument("--dataset-dir", type=_absolute_dir, required=True)
    build.add_argument("--archive-dir", type=_absolute_dir, required=True)
    build.add_argument("--known-at", type=_known_at, required=True)
    build.add_argument("--output-dir", type=_absolute_dir)
    build.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _protected_roots() -> tuple[Path, ...]:
    repository_root = Path(__file__).resolve().parents[1]
    data_root = Path("~/.local/share/investment-analyst").expanduser()
    return (
        repository_root,
        data_root / "workspaces",
        data_root / "runtime",
        data_root / "ops",
    )


def _print_acquire(receipt: FundingArchiveReceipt) -> None:
    print(f"archive_id={receipt.archive_id}")
    print(f"semantic_sha256={receipt.semantic_sha256}")
    print(f"status={receipt.status} dir={receipt.archive_dir}")
    print(
        "decisions={decision_count} points={observed_points}/{expected_points}"
        " missing={missing_points} pages={page_count}".format(**receipt.model_dump())
    )
    print(f"retrieved_at={receipt.retrieved_at.isoformat()}")
    print(f"terms={receipt.provider_terms_url}")


def _print_build(
    summary: FundingBundleSummary,
    receipt: FundingBundleReceipt | None,
) -> None:
    counts = summary.counts
    admission = summary.admission
    print(f"bundle_id={summary.bundle_id}")
    print(f"semantic_sha256={summary.bundle_content_sha256}")
    print(
        "feature_set_version={feature_set_version}"
        " availability_policy={availability_policy}".format(**summary.model_dump())
    )
    print(f"base_dataset={summary.base_dataset.dataset_id} archive={summary.archive.archive_id}")
    print(
        "rows={requested} base_eligible={base_eligible}"
        " joint={joint_available} base_joint={base_eligible_joint_available}".format(
            **counts.model_dump()
        )
    )
    print(
        f"admission={admission.status.value}"
        f" coverage={admission.coverage_numerator}/{admission.coverage_denominator}"
        f" fraction={admission.coverage_fraction}"
    )
    print(
        f"eligibility train={admission.train_joint_eligible}"
        f" validation={admission.validation_joint_eligible}"
        f" holdout={admission.holdout_joint_eligible}"
    )
    print(
        "unmet={unmet}".format(
            unmet="; ".join(admission.unmet_requirements) or "none",
        )
    )
    for split in summary.splits:
        print(
            "{split}: requested={requested} base_eligible={base_eligible}"
            " joint={joint_available} base_joint={base_eligible_joint_available}".format(
                split=split.split.value,
                **split.model_dump(exclude={"split"}),
            )
        )
    if receipt is not None:
        print(f"export={receipt.status} rows={receipt.row_count} dir={receipt.bundle_dir}")
        print(f"rows_sha256={receipt.rows_sha256} manifest_sha256={receipt.manifest_sha256}")


def _run_acquire(arguments: argparse.Namespace) -> int:
    loaded = load_base_dataset(arguments.dataset_dir)
    if arguments.asset_id != loaded.reference.asset_id:
        raise ValueError("--asset-id must match the base dataset asset")
    request = funding_archive_request(
        asset_id=arguments.asset_id,
        decision_start_date=loaded.reference.start_date,
        decision_end_date=loaded.reference.end_date,
    )
    client = DeribitClient(UrlLibHttpTransport())
    receipt = acquire_funding_archive(
        client,
        request,
        output_dir=arguments.output_dir,
        clock=lambda: datetime.now(UTC),
    )
    if arguments.as_json:
        print(json.dumps(receipt.to_json_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _print_acquire(receipt)
    return 0


def _run_build(arguments: argparse.Namespace) -> int:
    loaded = load_base_dataset(arguments.dataset_dir)
    archive = read_funding_archive(arguments.archive_dir)
    bundle = build_funding_bundle(
        base_dataset=loaded.reference,
        decisions=loaded.decisions,
        funding_asset=archive.manifest.asset,
        archive=archive,
        known_at=arguments.known_at,
    )
    receipt: FundingBundleReceipt | None = None
    if arguments.output_dir is not None:
        receipt = export_funding_bundle(
            bundle,
            archive=archive,
            output_dir=arguments.output_dir,
            forbidden_roots=_protected_roots() + (arguments.dataset_dir, arguments.archive_dir),
        )
    summary = funding_bundle_view_summary(bundle)
    if arguments.as_json:
        payload: dict[str, object] = {"bundle": summary.to_json_dict()}
        if receipt is not None:
            payload["export"] = receipt.to_json_dict()
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        _print_build(summary, receipt)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "acquire":
            return _run_acquire(arguments)
        return _run_build(arguments)
    except (
        AssetCatalogError,
        DeribitError,
        FundingArchiveError,
        FundingExportError,
        FundingFeatureError,
        HttpRequestError,
        ValueError,
        OSError,
    ) as error:
        print(
            json.dumps(
                {"error": str(error)},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return _EXIT_INVALID


__all__ = [
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
