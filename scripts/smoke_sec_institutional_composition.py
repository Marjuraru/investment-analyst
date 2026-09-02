#!/usr/bin/env python3
"""Run the finite real SEC smoke for the read-only 13F composition policy."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.application.cazatiburones_institutional_composition import (
    CazatiburonesInstitutionalCompositionApplication,
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
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--filer-cik", default=_CIK)
    arguments = parser.parse_args()
    if arguments.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    if arguments.filer_cik.zfill(10) != _CIK.zfill(10):
        raise RuntimeError("smoke is fixed to Berkshire Hathaway CIK 1067983")
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT is required")

    started_at = datetime.now(UTC)
    head, tree = _git_revision()
    workspace = WorkspaceService().initialize(arguments.workspace).paths.root
    location = StorageLocationRequest(workspace=workspace)
    reports = SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
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
    application = CazatiburonesInstitutionalCompositionApplication.create_default()
    first = application.query(manager_cik=arguments.filer_cik, known_at=known_at, location=location)
    second = application.query(
        manager_cik=arguments.filer_cik, known_at=known_at, location=location
    )
    if not first or first != second:
        raise RuntimeError("SEC smoke did not prove reproducible composition")
    with LocalStorage(storage_paths, read_only=True) as storage:
        if storage.raw_records.count() != raw_before:
            raise RuntimeError("composition query changed persisted evidence")

    os.environ.pop("SEC_USER_AGENT", None)
    prior = application.query(
        manager_cik=arguments.filer_cik,
        known_at=min(item.available_at for item in reports) - timedelta(microseconds=1),
        location=location,
    )
    if prior:
        raise RuntimeError("prior point-in-time cut did not hide the semantic artifacts")
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "head": head,
                "tree": tree,
                "started_at": started_at.isoformat(),
                "known_at": known_at.isoformat(),
                "accessions": _ACCESSIONS,
                "report_ids": [str(value) for value in report_ids],
                "first_enrichment": first_enrichment.model_dump(mode="json"),
                "second_enrichment": second_enrichment.model_dump(mode="json"),
                "effective_artifact_ids": [
                    str(item.effective_artifact_id) if item.effective_artifact_id else None
                    for item in first
                ],
                "statuses": [item.status for item in first],
                "ambiguities_detected": sum(item.status == "ambiguous" for item in first),
                "prior_results": len(prior),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
