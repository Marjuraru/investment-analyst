#!/usr/bin/env python3
"""Summarize or export the preregistered crypto daily predictive dataset."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.research.predictive_dataset import (
    PredictiveDatasetBuilder,
    PredictiveDatasetError,
)
from investment_analyst.research.predictive_export import (
    PredictiveExportError,
    PredictiveExportReceipt,
    export_predictive_dataset,
)
from investment_analyst.research.predictive_models import PredictiveDatasetRequest
from investment_analyst.storage.errors import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

_EXIT_INVALID = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compute a read-only predictive dataset summary or export one sealed"
            " Parquet artifact outside the operational workspace."
        )
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--known-at", type=_known_at, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _known_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--known-at must include timezone information")
    return parsed


def _receipt_payload(receipt: PredictiveExportReceipt) -> dict[str, object]:
    return receipt.to_json_dict()


def _print_human(
    dataset_payload: dict[str, object],
    receipt: PredictiveExportReceipt | None,
) -> None:
    asset = dataset_payload["asset"]
    counts = dataset_payload["counts"]
    readiness = dataset_payload["readiness"]
    splits = dataset_payload["splits"]
    assert isinstance(asset, dict)
    assert isinstance(counts, dict)
    assert isinstance(readiness, dict)
    assert isinstance(splits, list)
    print(f"dataset_id={dataset_payload['dataset_id']}")
    print(f"semantic_sha256={dataset_payload['dataset_content_sha256']}")
    print(f"asset={asset['asset_id']} source={asset['source_id']}")
    print(
        "decisions={requested} feature_available={feature_available} "
        "label_available={label_available} eligible={eligible}".format(**counts)
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


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        request = PredictiveDatasetRequest(
            asset_id=arguments.asset_id,
            start_date=arguments.start_date,
            end_date=arguments.end_date,
            dataset_known_at=arguments.known_at,
        )
        runtime = ApplicationRuntime.create_default()
        location_request = StorageLocationRequest(workspace=arguments.workspace)
        location = runtime.resolve_storage(
            location_request,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        )
        with runtime.open_storage(
            location_request,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            builder = PredictiveDatasetBuilder(
                storage,
                provider_resolver=runtime.provider_resolver,
                catalog=runtime.catalog,
            )
            dataset = builder.build(request)
            receipt = None
            if arguments.output_dir is not None:
                receipt = export_predictive_dataset(
                    dataset,
                    output_dir=arguments.output_dir,
                    workspace_root=location.root,
                )
        summary = dataset.summary.to_json_dict()
        if arguments.as_json:
            payload: dict[str, object] = {"dataset": summary}
            if receipt is not None:
                payload["export"] = _receipt_payload(receipt)
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            _print_human(summary, receipt)
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        ProviderAssetContextError,
        PredictiveDatasetError,
        PredictiveExportError,
        StorageError,
        WorkspaceError,
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


if __name__ == "__main__":
    raise SystemExit(main())
