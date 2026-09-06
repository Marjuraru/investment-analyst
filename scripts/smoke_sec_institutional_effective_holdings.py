#!/usr/bin/env python3
"""Run the finite real SEC smoke for the read-only 13F effective public holdings policy."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from investment_analyst.application.cazatiburones_institutional_effective_holdings import (
    CazatiburonesInstitutionalEffectiveHoldingsApplication,
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

# Official SEC Accessions for Berkshire Hathaway (CIK 1067983)
# 1. Restatement chain: Period 2023-09-30 (Original + RESTATEMENT)
_RESTATEMENT_ACCESSIONS = ("0000950123-23-010898", "0000950123-23-011029")
_RESTATEMENT_PERIOD = date(2023, 9, 30)

# 2. New Holdings chain: Berkshire Hathaway Q2 2025 (Original + NEW HOLDINGS)
_NEW_HOLDINGS_ACCESSIONS = ("0000950123-25-005701", "0000950123-25-008361")
_NEW_HOLDINGS_PERIOD = date(2025, 3, 31)

_ALL_ACCESSIONS = _RESTATEMENT_ACCESSIONS + _NEW_HOLDINGS_ACCESSIONS
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


def _get_user_agent() -> str:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if user_agent:
        return user_agent
    env_paths = (
        Path(__file__).resolve().parents[1] / ".env",
        Path("/home/marjuraru/projects/investment-analyst/.env"),
    )
    for env_path in env_paths:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("SEC_USER_AGENT="):
                    val = stripped.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        os.environ["SEC_USER_AGENT"] = val
                        return val
    raise RuntimeError("SEC_USER_AGENT is required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--filer-cik", default=_CIK)
    arguments = parser.parse_args()
    if arguments.workspace.exists():
        raise RuntimeError("workspace must be a new scratch path")
    if arguments.filer_cik.zfill(10) != _CIK.zfill(10):
        raise RuntimeError("smoke is fixed to Berkshire Hathaway CIK 1067983")
    user_agent = _get_user_agent()

    started_at = datetime.now(UTC)
    head, tree = _git_revision()
    workspace = WorkspaceService().initialize(arguments.workspace).paths.root
    location = StorageLocationRequest(workspace=workspace)

    # 1. Import institutional holdings from SEC EDGAR
    reports = SecInstitutionalHoldingsApplication.create_default().import_institutional_holdings(
        request=sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik=arguments.filer_cik, accessions=_ALL_ACCESSIONS
        ),
        location=location,
        sec_identity=SecEdgarIdentity(user_agent),
    )
    if len(reports) != len(_ALL_ACCESSIONS):
        raise RuntimeError("SEC smoke did not persist all fixed institutional reports")
    report_ids = tuple(item.report_id for item in reports)
    reports_by_accession = {item.cover_revision.document.filing.accession: item for item in reports}

    # 2. Enrich semantics
    known_at = datetime.now(UTC)
    semantics = SecInstitutionalSemanticsApplication.create_default()
    request = InstitutionalSemanticsEnrichRequest(
        manager_cik=arguments.filer_cik, report_ids=report_ids, known_at=known_at
    )
    first_enrichment = semantics.enrich(request=request, location=location)
    second_enrichment = semantics.enrich(request=request, location=location)
    if first_enrichment.created != len(_ALL_ACCESSIONS) or second_enrichment.reused != len(
        _ALL_ACCESSIONS
    ):
        raise RuntimeError("SEC smoke did not prove semantic enrichment idempotence")

    # 3. Capture baseline storage state before read-only queries
    storage_paths = StoragePaths.from_root(WorkspaceService().resolve(workspace).storage_root)
    with LocalStorage(storage_paths, read_only=True) as storage:
        raw_before = storage.raw_records.count()

    effective_app = CazatiburonesInstitutionalEffectiveHoldingsApplication.create_default()

    # 4. Validate Restatement chain
    amend_restatement = reports_by_accession[_RESTATEMENT_ACCESSIONS[1]]
    # Prior to amendment cut: only original visible
    cut_before_restatement = amend_restatement.available_at - timedelta(microseconds=1)
    res_before_restatement = effective_app.query(
        manager_cik=arguments.filer_cik,
        report_period=_RESTATEMENT_PERIOD,
        known_at=cut_before_restatement,
        offset=0,
        limit=1000,
        location=location,
    )
    # After amendment cut: restatement replaces original
    cut_after_restatement = amend_restatement.available_at + timedelta(seconds=1)
    res_after_restatement = effective_app.query(
        manager_cik=arguments.filer_cik,
        report_period=_RESTATEMENT_PERIOD,
        known_at=cut_after_restatement,
        offset=0,
        limit=1000,
        location=location,
    )
    if res_before_restatement.status != "effective" or res_after_restatement.status != "effective":
        raise RuntimeError("Restatement chain query failed to return effective status")
    if (
        len(res_before_restatement.contributors) != 1
        or res_before_restatement.contributors[0].accession != _RESTATEMENT_ACCESSIONS[0]
    ):
        raise RuntimeError("Before restatement did not select original contributor")
    if (
        len(res_after_restatement.contributors) != 1
        or res_after_restatement.contributors[0].accession != _RESTATEMENT_ACCESSIONS[1]
    ):
        raise RuntimeError("After restatement did not replace contributor with restatement")

    # 5. Validate New Holdings chain (Berkshire Hathaway Q2 2025)
    amend_new_holdings = reports_by_accession[_NEW_HOLDINGS_ACCESSIONS[1]]
    cut_before_new_holdings = amend_new_holdings.available_at - timedelta(microseconds=1)
    res_before_new_holdings = effective_app.query(
        manager_cik=arguments.filer_cik,
        report_period=_NEW_HOLDINGS_PERIOD,
        known_at=cut_before_new_holdings,
        offset=0,
        limit=1000,
        location=location,
    )
    cut_after_new_holdings = amend_new_holdings.available_at + timedelta(seconds=1)
    res_after_new_holdings = effective_app.query(
        manager_cik=arguments.filer_cik,
        report_period=_NEW_HOLDINGS_PERIOD,
        known_at=cut_after_new_holdings,
        offset=0,
        limit=1000,
        location=location,
    )
    if (
        res_before_new_holdings.status != "effective"
        or res_after_new_holdings.status != "effective"
    ):
        raise RuntimeError("New holdings chain query failed to return effective status")
    if res_before_new_holdings.total_rows != 110:
        raise RuntimeError(
            "Expected 110 rows before Berkshire amendment, "
            f"got {res_before_new_holdings.total_rows}"
        )
    if res_after_new_holdings.total_rows != 114:
        raise RuntimeError(
            f"Expected 114 rows after Berkshire amendment, got {res_after_new_holdings.total_rows}"
        )
    if len(res_after_new_holdings.contributors) != 2:
        raise RuntimeError("Expected 2 contributors after Berkshire amendment (base + supplement)")
    if (
        res_after_new_holdings.contributors[0].accession != _NEW_HOLDINGS_ACCESSIONS[0]
        or res_after_new_holdings.contributors[1].accession != _NEW_HOLDINGS_ACCESSIONS[1]
    ):
        raise RuntimeError("Berkshire contributors lineage mismatch")

    # 6. Verify exact repetition / idempotence
    repeated = effective_app.query(
        manager_cik=arguments.filer_cik,
        report_period=_NEW_HOLDINGS_PERIOD,
        known_at=cut_after_new_holdings,
        offset=0,
        limit=1000,
        location=location,
    )
    if repeated != res_after_new_holdings:
        raise RuntimeError("Repeated effective holdings query is not identical")

    # 7. Verify storage remained byte-identical / unmodified
    with LocalStorage(storage_paths, read_only=True) as storage:
        if storage.raw_records.count() != raw_before:
            raise RuntimeError("Effective holdings queries modified persisted storage")

    # 8. Verify point-in-time cut prior to any filing returns insufficient
    earliest_available = min(item.available_at for item in reports)
    prior_cut = earliest_available - timedelta(microseconds=1)
    prior_result = effective_app.query(
        manager_cik=arguments.filer_cik,
        report_period=_NEW_HOLDINGS_PERIOD,
        known_at=prior_cut,
        offset=0,
        limit=1000,
        location=location,
    )
    if prior_result.status != "insufficient" or len(prior_result.rows) != 0:
        raise RuntimeError("Prior point-in-time cut did not hide artifacts")

    os.environ.pop("SEC_USER_AGENT", None)

    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "head": head,
                "tree": tree,
                "started_at": started_at.isoformat(),
                "known_at": known_at.isoformat(),
                "accessions": _ALL_ACCESSIONS,
                "report_ids": [str(value) for value in report_ids],
                "first_enrichment": first_enrichment.model_dump(mode="json"),
                "second_enrichment": second_enrichment.model_dump(mode="json"),
                "berkshire_q2_2025_rows_before": res_before_new_holdings.total_rows,
                "berkshire_q2_2025_rows_after": res_after_new_holdings.total_rows,
                "restatement_rows_before": res_before_restatement.total_rows,
                "restatement_rows_after": res_after_restatement.total_rows,
                "repetition_identical": repeated == res_after_new_holdings,
                "storage_raw_unmodified": True,
                "prior_cut_insufficient": prior_result.status == "insufficient",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
