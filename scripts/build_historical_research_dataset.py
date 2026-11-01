#!/usr/bin/env python3
"""Acquire a sealed Coinbase history archive or build its reconstructed dataset."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.http import HttpRequestError, UrlLibHttpTransport
from investment_analyst.research.historical_archive import (
    HistoricalArchiveError,
    HistoricalArchiveReceipt,
    acquire_historical_archive,
    read_historical_archive,
)
from investment_analyst.research.historical_dataset import (
    HistoricalDatasetError,
    build_historical_dataset,
)
from investment_analyst.research.historical_export import (
    HistoricalExportError,
    HistoricalExportReceipt,
    export_historical_dataset,
)
from investment_analyst.research.historical_models import (
    HistoricalArchiveRequest,
    HistoricalDecisionRequest,
)

_EXIT_INVALID = 2


def _known_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--known-at must include timezone information")
    return parsed.astimezone(UTC)


def _absolute_archive_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("--archive-dir must be an absolute path")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire a finite Coinbase daily history archive or rebuild a causal research"
            " dataset offline. This command never opens the operational workspace."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire = subparsers.add_parser(
        "acquire",
        help="Fetch and seal one bounded Coinbase daily archive.",
    )
    acquire.add_argument("--asset-id", required=True)
    acquire.add_argument("--start-date", type=date.fromisoformat, required=True)
    acquire.add_argument("--end-date", type=date.fromisoformat, required=True)
    acquire.add_argument("--output-dir", type=Path, required=True)
    acquire.add_argument("--json", action="store_true", dest="as_json")

    build = subparsers.add_parser(
        "build",
        help="Reconstruct the dataset from a sealed archive, optionally exporting it.",
    )
    build.add_argument("--archive-dir", type=_absolute_archive_dir, required=True)
    build.add_argument("--start-date", type=date.fromisoformat, required=True)
    build.add_argument("--end-date", type=date.fromisoformat, required=True)
    build.add_argument("--known-at", type=_known_at, required=True)
    build.add_argument("--output-dir", type=Path)
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


def _print_acquire(receipt: HistoricalArchiveReceipt) -> None:
    print(f"archive_id={receipt.archive_id}")
    print(f"semantic_sha256={receipt.semantic_sha256}")
    print(f"status={receipt.status} dir={receipt.archive_dir}")
    print(
        "requested_days={requested_days} bars={bar_count} absent={absent_days}"
        " requests={request_count}".format(**receipt.model_dump())
    )
    print(f"retrieved_at={receipt.retrieved_at.isoformat()}")
    print(f"terms={receipt.provider_terms_url}")


def _print_build(
    summary: dict[str, object],
    receipt: HistoricalExportReceipt | None,
) -> None:
    counts = summary["counts"]
    readiness = summary["readiness"]
    splits = summary["splits"]
    assert isinstance(counts, dict)
    assert isinstance(readiness, dict)
    assert isinstance(splits, list)
    print(f"dataset_id={summary['dataset_id']}")
    print(f"semantic_sha256={summary['dataset_content_sha256']}")
    print(
        "availability_basis={basis} strict_pit_verified={pit}".format(
            basis=summary["availability_basis"],
            pit=summary["strict_pit_verified"],
        )
    )
    print(
        "decisions={requested} features={feature_available} labels={label_available}"
        " eligible={eligible_before_split} purged={purged}".format(**counts)
    )
    print(
        "readiness={status} unmet={unmet}".format(
            status=readiness["status"],
            unmet="; ".join(readiness["unmet_requirements"]) or "none",
        )
    )
    for split in splits:
        print(
            "{split}: proposed={proposed} purged={purged} eligible={eligible_after_purge}".format(
                **split
            )
        )
    if receipt is not None:
        print(f"export={receipt.status} rows={receipt.row_count} dir={receipt.dataset_dir}")
        print(f"rows_sha256={receipt.rows_sha256} manifest_sha256={receipt.manifest_sha256}")


def _run_acquire(arguments: argparse.Namespace) -> int:
    request = HistoricalArchiveRequest(
        asset_id=arguments.asset_id,
        start_date=arguments.start_date,
        end_date=arguments.end_date,
    )
    client = CoinbaseExchangeClient(UrlLibHttpTransport())
    receipt = acquire_historical_archive(
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
    archive = read_historical_archive(arguments.archive_dir)
    request = HistoricalDecisionRequest(
        asset_id=archive.manifest.asset.asset_id,
        start_date=arguments.start_date,
        end_date=arguments.end_date,
        known_at=arguments.known_at,
    )
    dataset = build_historical_dataset(request, archive)
    receipt: HistoricalExportReceipt | None = None
    if arguments.output_dir is not None:
        receipt = export_historical_dataset(
            dataset,
            archive=archive,
            output_dir=arguments.output_dir,
            forbidden_roots=_protected_roots(),
        )
    summary = dataset.summary.to_json_dict()
    if arguments.as_json:
        payload: dict[str, object] = {"dataset": summary}
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
        HistoricalArchiveError,
        HistoricalDatasetError,
        HistoricalExportError,
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
