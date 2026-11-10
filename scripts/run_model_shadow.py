#!/usr/bin/env python3
"""Run or report one explicit, bounded research model shadow observation.

Without ``--enabled`` the command reports ``disabled`` and touches neither the workspace, the
artifact, nor the shadow root. Enabling it requires every runtime argument explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.catalog.provider_context import ProviderAssetContextError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.research.feature_preview_models import FeaturePreviewError
from investment_analyst.research.inference_models import ModelArtifactError
from investment_analyst.research.shadow_models import (
    SHADOW_RUN_SCHEMA_VERSION,
    ShadowRunRequest,
    ShadowRunSummary,
)
from investment_analyst.research.shadow_service import (
    ModelShadowService,
    ModelShadowServiceError,
)
from investment_analyst.research.shadow_store import ShadowStoreError
from investment_analyst.storage.errors import StorageError
from investment_analyst.workspace.service import WorkspaceError

_EXIT_INVALID = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Observe the isolated constant test inference under an explicit, bounded shadow."
            " Disabled by default; no training, acquisition, promotion or alert output."
        )
    )
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--model-artifact", type=Path)
    parser.add_argument("--asset-id", action="append", dest="asset_ids")
    parser.add_argument("--decision-date", type=_date_value)
    parser.add_argument("--known-at", type=_known_at)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _date_value(value: str) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise argparse.ArgumentTypeError("--decision-date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--decision-date must use YYYY-MM-DD") from error


def _known_at(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--known-at must include timezone information")
    return parsed.astimezone(UTC)


def _print_human(summary: ShadowRunSummary) -> None:
    print("enabled=true")
    print(f"status={summary.status}")
    print(f"run_id={summary.run_id}")
    print(f"manifest={summary.manifest_path}")
    print(f"manifest_sha256={summary.manifest_sha256}")
    for asset in summary.assets:
        probability = "null" if asset.test_probability is None else str(asset.test_probability)
        print(
            f"asset={asset.asset_id} source={asset.source_id} status={asset.status.value} "
            f"inference_id={asset.inference_id} inference_sha256={asset.inference_sha256} "
            f"test_probability={probability}"
        )


def _enabled_arguments(arguments: argparse.Namespace) -> list[str]:
    required = {
        "--workspace": arguments.workspace,
        "--model-artifact": arguments.model_artifact,
        "--asset-id": arguments.asset_ids,
        "--decision-date": arguments.decision_date,
        "--known-at": arguments.known_at,
        "--output-root": arguments.output_root,
    }
    return [name for name, value in required.items() if value is None]


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.enabled:
        print(
            json.dumps(
                {
                    "enabled": False,
                    "schema_version": SHADOW_RUN_SCHEMA_VERSION,
                    "status": "disabled",
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    missing = _enabled_arguments(arguments)
    if missing:
        print(
            json.dumps(
                {"error": f"--enabled requires {', '.join(missing)}"},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return _EXIT_INVALID
    try:
        request = ShadowRunRequest(
            workspace=arguments.workspace,
            model_artifact=arguments.model_artifact,
            asset_ids=tuple(arguments.asset_ids),
            decision_date=arguments.decision_date,
            known_at=arguments.known_at,
            output_root=arguments.output_root,
        )
        summary = ModelShadowService().run(request)
        if arguments.as_json:
            print(
                json.dumps(
                    {"enabled": True, **summary.to_json_dict()},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            _print_human(summary)
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        FeaturePreviewError,
        ModelArtifactError,
        ModelShadowServiceError,
        ProviderAssetContextError,
        ShadowStoreError,
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
