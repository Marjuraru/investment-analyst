#!/usr/bin/env python3
"""Run the finite real SEC smoke for read-only declared 13F concentration."""

import argparse
import json
import os
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.application.cazatiburones_institutional_concentration import (
    CazatiburonesInstitutionalConcentrationApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.sec_institutional_holdings import (
    SecInstitutionalHoldingsApplication,
)
from investment_analyst.application.sec_institutional_semantics import (
    SecInstitutionalSemanticsApplication,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ACCESSIONS = ("0001193125-26-226661", "0001193125-26-352200")
_CIK = "1067983"


def _git_revision() -> tuple[str, str]:
    repository = Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head, tree


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-parent", type=Path)
    parser.add_argument("--filer-cik", default=_CIK)
    arguments = parser.parse_args()
    if arguments.filer_cik.zfill(10) != _CIK.zfill(10):
        raise RuntimeError("smoke is fixed to Berkshire Hathaway CIK 1067983")
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT is required")
    if arguments.workspace_parent is not None and not arguments.workspace_parent.is_dir():
        raise RuntimeError("workspace-parent must be an existing directory")

    started_at = datetime.now(UTC)
    head, tree = _git_revision()
    with tempfile.TemporaryDirectory(
        prefix="investment-analyst-sec-concentration-",
        dir=arguments.workspace_parent,
    ) as scratch:
        workspace = WorkspaceService().initialize(Path(scratch) / "workspace").paths.root
        location = StorageLocationRequest(workspace=workspace)
        holdings = SecInstitutionalHoldingsApplication.create_default()
        reports = holdings.import_institutional_holdings(
            request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                filer_cik=arguments.filer_cik, accessions=_ACCESSIONS
            ),
            location=location,
            sec_identity=SecEdgarIdentity(user_agent),
        )
        if len(reports) != len(_ACCESSIONS):
            raise RuntimeError("SEC smoke did not persist both fixed institutional reports")
        report_ids = tuple(item.report_id for item in reports)
        known_at = datetime.now(UTC)
        semantics = SecInstitutionalSemanticsApplication.create_default()
        request = InstitutionalSemanticsEnrichRequest(
            manager_cik=arguments.filer_cik, report_ids=report_ids, known_at=known_at
        )
        first_enrichment = semantics.enrich(request=request, location=location)
        second_enrichment = semantics.enrich(request=request, location=location)
        if first_enrichment.created != 2 or second_enrichment.reused != 2:
            raise RuntimeError("SEC smoke did not prove semantic enrichment idempotence")

        storage_paths = StoragePaths.from_root(WorkspaceService().resolve(workspace).storage_root)
        with LocalStorage(storage_paths, read_only=True) as storage:
            raw_before = storage.raw_records.count()
        application = CazatiburonesInstitutionalConcentrationApplication.create_default()
        first = application.query(
            manager_cik=arguments.filer_cik, known_at=known_at, location=location
        )
        second = application.query(
            manager_cik=arguments.filer_cik, known_at=known_at, location=location
        )
        if not first or first != second:
            raise RuntimeError("SEC smoke did not prove reproducible declared concentration")
        with LocalStorage(storage_paths, read_only=True) as storage:
            if storage.raw_records.count() != raw_before:
                raise RuntimeError("concentration query changed persisted evidence")

        os.environ.pop("SEC_USER_AGENT", None)
        prior = application.query(
            manager_cik=arguments.filer_cik,
            known_at=min(item.available_at for item in reports) - timedelta(microseconds=1),
            location=location,
        )
        if prior:
            raise RuntimeError("prior point-in-time cut did not hide the semantic artifacts")
        payload = {
            "accessions": _ACCESSIONS,
            "calculated": sum(item.status == "calculated" for item in first),
            "effective_artifact_ids": [
                str(item.effective_artifact_id) if item.effective_artifact_id else None
                for item in first
            ],
            "head": head,
            "known_at": known_at.isoformat(),
            "magnitudes": [
                {
                    "herfindahl_index": str(item.herfindahl_index)
                    if item.herfindahl_index is not None
                    else None,
                    "largest_declared_weight": str(item.largest_declared_weight)
                    if item.largest_declared_weight is not None
                    else None,
                    "position_count": item.position_count,
                    "top_five_declared_weight": str(item.top_five_declared_weight)
                    if item.top_five_declared_weight is not None
                    else None,
                    "top_ten_declared_weight": str(item.top_ten_declared_weight)
                    if item.top_ten_declared_weight is not None
                    else None,
                }
                for item in first
            ],
            "omissions_by_reason": dict(
                sorted(Counter(item.reason for item in first if item.status == "omitted").items())
            ),
            "prior_results": len(prior),
            "second_query_identical": first == second,
            "started_at": started_at.isoformat(),
            "tree": tree,
            "workspace_discarded": True,
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
