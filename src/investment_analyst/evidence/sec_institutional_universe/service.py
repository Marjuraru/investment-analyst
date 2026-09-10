"""Closed, deterministic parser and manager-universe selection service."""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from uuid import UUID

from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_universe.identity import (
    SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION,
    SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
    SEC_13F_MANAGER_UNIVERSE_SELECTION_POLICY,
    candidate_id,
)
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    ALLOWED_SUBMISSION_FORMS,
    Sec13FDataSetDownload,
    Sec13FDataSetError,
    parse_sec_date_value,
    validate_sec_13f_zip_archive,
)


class SecInstitutionalUniverseServiceError(Sec13FDataSetError):
    """Failure parsing tabular dataset files or applying selection policy."""


@dataclass(frozen=True, slots=True)
class _SubmissionEntry:
    accession: str
    filing_date: date
    form: str
    cik: str
    period_of_report: date


@dataclass(frozen=True, slots=True)
class _CoverPageEntry:
    accession: str
    manager_name: str
    is_amendment: bool


class SecInstitutionalUniverseService:
    """Parse official Form 13F dataset archives and compute deterministic universe snapshots."""

    def __init__(self, policy_version: str = SEC_13F_MANAGER_UNIVERSE_SELECTION_POLICY) -> None:
        self._policy_version = policy_version

    def build_universe_from_download(
        self,
        download: Sec13FDataSetDownload,
        *,
        catalog_cusips: dict[str, str],
        catalog_version: int | str,
        max_managers_per_asset: int = 25,
    ) -> tuple[Sec13FDataSetRevision, Sec13FManagerUniverseSnapshot]:
        """Build a revision and selection snapshot from a verified dataset download."""
        if not catalog_cusips:
            raise SecInstitutionalUniverseServiceError("catalog_cusips mapping must not be empty")

        # 1. Create dataset revision
        revision = Sec13FDataSetRevision.create(
            dataset_url=download.url,
            period_start=download.period_start,
            period_end=download.period_end,
            content_sha256=download.sha256,
            size_bytes=download.size_bytes,
            retrieved_at=download.retrieved_at,
            schema_version=SEC_13F_DATA_SET_REVISION_SCHEMA_VERSION,
        )

        # 2. Parse and evaluate universe
        snapshot = self.build_universe_snapshot(
            download.content,
            dataset_revision=revision,
            catalog_cusips=catalog_cusips,
            catalog_version=catalog_version,
            max_managers_per_asset=max_managers_per_asset,
        )
        return revision, snapshot

    def build_universe_snapshot(
        self,
        zip_bytes: bytes,
        *,
        dataset_revision: Sec13FDataSetRevision,
        catalog_cusips: dict[str, str],
        catalog_version: int | str,
        max_managers_per_asset: int = 25,
    ) -> Sec13FManagerUniverseSnapshot:
        """Parse ZIP tables and apply selection policy without modifying existing state."""
        validate_sec_13f_zip_archive(zip_bytes)

        stream = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(stream, "r") as archive:
            submissions_raw = self._read_member(archive, "SUBMISSION.tsv")
            coverpage_raw = self._read_member(archive, "COVERPAGE.tsv")
            infotable_stream = self._open_member_stream(archive, "INFOTABLE.tsv")

            # Parse SUBMISSION.tsv
            submissions_by_acc, all_accessions = self._parse_submissions(submissions_raw)

            # Parse COVERPAGE.tsv
            coverpage_by_acc = self._parse_coverpage(coverpage_raw, all_accessions)

            # Stream and filter INFOTABLE.tsv
            # Mapping: (asset_id, cusip, cik, accession) -> sum(Decimal(value))
            holdings_by_accession = self._stream_infotable(
                infotable_stream,
                catalog_cusips=catalog_cusips,
                valid_submissions=submissions_by_acc,
                all_known_accessions=all_accessions,
            )

        # Apply selection policy
        (
            candidates,
            eligible_count,
            matched_count,
            selected_count,
            unselected_count,
            covered,
            missing,
            max_filing_date,
        ) = self._apply_selection_policy(
            holdings_by_accession=holdings_by_accession,
            submissions_by_acc=submissions_by_acc,
            coverpage_by_acc=coverpage_by_acc,
            catalog_cusips=catalog_cusips,
            dataset_sha256=dataset_revision.content_sha256,
            dataset_revision_id=dataset_revision.revision_id,
            max_managers_per_asset=max_managers_per_asset,
        )

        event_time_date = max_filing_date or dataset_revision.period_end
        event_time = datetime.combine(event_time_date, time.min, tzinfo=UTC)

        return Sec13FManagerUniverseSnapshot.create(
            dataset_revision_id=dataset_revision.revision_id,
            dataset_sha256=dataset_revision.content_sha256,
            catalog_version=catalog_version,
            period_start=dataset_revision.period_start,
            period_end=dataset_revision.period_end,
            retrieved_at=dataset_revision.retrieved_at,
            event_time=event_time,
            eligible_asset_count=eligible_count,
            matched_asset_count=matched_count,
            candidate_manager_count=len(candidates),
            selected_manager_count=selected_count,
            unselected_manager_count=unselected_count,
            max_managers_per_asset=max_managers_per_asset,
            coverage_complete=bool(unselected_count == 0 and len(missing) == 0),
            covered_cusips=covered,
            missing_cusips=missing,
            candidates=candidates,
            policy_version=self._policy_version,
            schema_version=SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
        )

    def _read_member(self, archive: zipfile.ZipFile, member_name: str) -> str:
        try:
            raw = archive.read(member_name)
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SecInstitutionalUniverseServiceError(
                f"ZIP member {member_name} contains non-UTF-8 bytes"
            ) from error
        except KeyError as error:
            raise SecInstitutionalUniverseServiceError(
                f"Required ZIP member {member_name} not found"
            ) from error

    def _open_member_stream(self, archive: zipfile.ZipFile, member_name: str) -> io.TextIOWrapper:
        try:
            raw_stream = archive.open(member_name, "r")
            return io.TextIOWrapper(raw_stream, encoding="utf-8", errors="strict")
        except KeyError as error:
            raise SecInstitutionalUniverseServiceError(
                f"Required ZIP member {member_name} not found"
            ) from error

    def _parse_submissions(self, tsv_text: str) -> tuple[dict[str, _SubmissionEntry], set[str]]:
        reader = csv.reader(io.StringIO(tsv_text), delimiter="\t")
        try:
            header = next(reader)
        except StopIteration as error:
            raise SecInstitutionalUniverseServiceError("SUBMISSION.tsv is empty") from error

        header_indices = {col.strip().upper(): i for i, col in enumerate(header)}
        required = ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT")
        for col in required:
            if col not in header_indices:
                raise SecInstitutionalUniverseServiceError(
                    f"SUBMISSION.tsv missing required header {col}"
                )

        submissions: dict[str, _SubmissionEntry] = {}
        all_accessions: set[str] = set()

        acc_idx = header_indices["ACCESSION_NUMBER"]
        date_idx = header_indices["FILING_DATE"]
        type_idx = header_indices["SUBMISSIONTYPE"]
        cik_idx = header_indices["CIK"]
        period_idx = header_indices["PERIODOFREPORT"]

        for row_number, row in enumerate(reader, start=2):
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) <= max(acc_idx, date_idx, type_idx, cik_idx, period_idx):
                raise SecInstitutionalUniverseServiceError(
                    f"SUBMISSION.tsv row {row_number} has fewer columns than header"
                )

            accession = row[acc_idx].strip()
            if not accession:
                raise SecInstitutionalUniverseServiceError(
                    f"Empty accession in SUBMISSION.tsv row {row_number}"
                )

            if accession in all_accessions:
                raise SecInstitutionalUniverseServiceError(
                    f"Duplicate accession {accession} in SUBMISSION.tsv row {row_number}"
                )
            all_accessions.add(accession)

            form = row[type_idx].strip().upper()
            if form not in ALLOWED_SUBMISSION_FORMS:
                # Exclude 13F-NT and others
                continue

            try:
                filing_date = parse_sec_date_value(row[date_idx])
                period_of_report = parse_sec_date_value(row[period_idx])
            except Exception as error:
                raise SecInstitutionalUniverseServiceError(
                    f"Invalid date in SUBMISSION.tsv row {row_number}"
                ) from error

            try:
                cik = normalize_cik(row[cik_idx].strip())
            except Exception as error:
                raise SecInstitutionalUniverseServiceError(
                    f"Invalid CIK in SUBMISSION.tsv row {row_number}"
                ) from error

            submissions[accession] = _SubmissionEntry(
                accession=accession,
                filing_date=filing_date,
                form=form,
                cik=cik,
                period_of_report=period_of_report,
            )

        return submissions, all_accessions

    def _parse_coverpage(
        self, tsv_text: str, all_accessions: set[str]
    ) -> dict[str, _CoverPageEntry]:
        reader = csv.reader(io.StringIO(tsv_text), delimiter="\t")
        try:
            header = next(reader)
        except StopIteration as error:
            raise SecInstitutionalUniverseServiceError("COVERPAGE.tsv is empty") from error

        header_indices = {col.strip().upper(): i for i, col in enumerate(header)}
        for col in ("ACCESSION_NUMBER", "FILINGMANAGER_NAME"):
            if col not in header_indices:
                raise SecInstitutionalUniverseServiceError(
                    f"COVERPAGE.tsv missing required header {col}"
                )

        coverpages: dict[str, _CoverPageEntry] = {}
        acc_idx = header_indices["ACCESSION_NUMBER"]
        name_idx = header_indices["FILINGMANAGER_NAME"]
        amend_idx = header_indices.get("ISAMENDMENT")

        for row_number, row in enumerate(reader, start=2):
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) <= max(acc_idx, name_idx):
                raise SecInstitutionalUniverseServiceError(
                    f"COVERPAGE.tsv row {row_number} has fewer columns than required"
                )

            accession = row[acc_idx].strip()
            if not accession:
                raise SecInstitutionalUniverseServiceError(
                    f"Empty accession in COVERPAGE.tsv row {row_number}"
                )

            if accession not in all_accessions:
                raise SecInstitutionalUniverseServiceError(
                    f"COVERPAGE.tsv row {row_number} references orphaned accession {accession}"
                )

            manager_name = row[name_idx].strip()
            if not manager_name:
                raise SecInstitutionalUniverseServiceError(
                    f"Empty manager name in COVERPAGE.tsv row {row_number}"
                )

            is_amendment = False
            if amend_idx is not None and len(row) > amend_idx:
                is_amendment = row[amend_idx].strip().upper() == "Y"

            if accession in coverpages:
                raise SecInstitutionalUniverseServiceError(
                    f"Duplicate accession {accession} in COVERPAGE.tsv row {row_number}"
                )

            coverpages[accession] = _CoverPageEntry(
                accession=accession,
                manager_name=manager_name,
                is_amendment=is_amendment,
            )

        return coverpages

    def _stream_infotable(
        self,
        stream: io.TextIOWrapper,
        *,
        catalog_cusips: dict[str, str],
        valid_submissions: dict[str, _SubmissionEntry],
        all_known_accessions: set[str],
    ) -> dict[tuple[str, str, str, str], Decimal]:
        """Stream INFOTABLE.tsv, filter by authorized CUSIPs, and aggregate Decimal values."""
        reader = csv.reader(stream, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration as error:
            raise SecInstitutionalUniverseServiceError("INFOTABLE.tsv is empty") from error

        header_indices = {col.strip().upper(): i for i, col in enumerate(header)}
        for col in ("ACCESSION_NUMBER", "CUSIP", "VALUE"):
            if col not in header_indices:
                raise SecInstitutionalUniverseServiceError(
                    f"INFOTABLE.tsv missing required header {col}"
                )

        acc_idx = header_indices["ACCESSION_NUMBER"]
        cusip_idx = header_indices["CUSIP"]
        value_idx = header_indices["VALUE"]

        # Result: (asset_id, cusip, cik, accession) -> sum(Decimal(value))
        totals: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)

        # Normalize target CUSIPs
        normalized_target_cusips = {
            cusip.upper().strip(): asset_id for cusip, asset_id in catalog_cusips.items()
        }

        for row_number, row in enumerate(reader, start=2):
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) <= max(acc_idx, cusip_idx, value_idx):
                raise SecInstitutionalUniverseServiceError(
                    f"INFOTABLE.tsv row {row_number} has fewer columns than required"
                )

            cusip = row[cusip_idx].upper().strip()
            if cusip not in normalized_target_cusips:
                # Discard non-catalog rows immediately to preserve streaming bounds
                continue

            accession = row[acc_idx].strip()
            if not accession:
                raise SecInstitutionalUniverseServiceError(
                    f"Empty accession in INFOTABLE.tsv row {row_number}"
                )

            if accession not in all_known_accessions:
                raise SecInstitutionalUniverseServiceError(
                    f"INFOTABLE.tsv row {row_number} references orphaned accession {accession}"
                )

            # Check if this accession belongs to an authorized 13F-HR / 13F-HR/A submission
            submission = valid_submissions.get(accession)
            if submission is None:
                # e.g., accession belongs to 13F-NT which was filtered out
                continue

            value_str = row[value_idx].strip()
            if not value_str:
                raise SecInstitutionalUniverseServiceError(
                    f"Empty value in INFOTABLE.tsv row {row_number}"
                )
            try:
                val = Decimal(value_str)
                if val < 0:
                    raise ValueError("Negative value")
            except (InvalidOperation, ValueError) as error:
                raise SecInstitutionalUniverseServiceError(
                    f"Invalid Decimal value '{value_str}' in INFOTABLE.tsv row {row_number}"
                ) from error

            asset_id = normalized_target_cusips[cusip]
            key = (asset_id, cusip, submission.cik, accession)
            totals[key] += val

        return totals

    def _apply_selection_policy(
        self,
        *,
        holdings_by_accession: dict[tuple[str, str, str, str], Decimal],
        submissions_by_acc: dict[str, _SubmissionEntry],
        coverpage_by_acc: dict[str, _CoverPageEntry],
        catalog_cusips: dict[str, str],
        dataset_sha256: str,
        dataset_revision_id: UUID,
        max_managers_per_asset: int = 25,
    ) -> tuple[
        tuple[Sec13FManagerCandidate, ...],
        int,
        int,
        int,
        int,
        tuple[str, ...],
        tuple[str, ...],
        date | None,
    ]:
        """Apply selection: latest period, sum by accession, max accession, top 25."""
        # Group entries by asset:
        # asset_id -> list of ((cusip, cik, accession), value)
        rows_by_asset: dict[str, list[tuple[str, str, str, Decimal]]] = defaultdict(list)
        for (asset_id, cusip, cik, accession), val in holdings_by_accession.items():
            rows_by_asset[asset_id].append((cusip, cik, accession, val))

        eligible_assets = sorted(set(catalog_cusips.values()))
        eligible_count = len(eligible_assets)
        matched_count = 0

        covered_cusips: list[str] = []
        missing_cusips: list[str] = []

        all_candidates: list[Sec13FManagerCandidate] = []
        selected_count = 0
        unselected_count = 0
        max_filing_date: date | None = None

        for asset_id in eligible_assets:
            asset_cusips = [c.upper().strip() for c, a in catalog_cusips.items() if a == asset_id]
            rows = rows_by_asset.get(asset_id, [])
            if not rows:
                for c in asset_cusips:
                    missing_cusips.append(c)
                continue

            # Find the latest PERIODOFREPORT present for this asset
            latest_period = max(submissions_by_acc[acc].period_of_report for (_, _, acc, _) in rows)

            # Filter rows for this asset strictly to the latest period
            period_rows = [
                (cusip, cik, acc, val)
                for (cusip, cik, acc, val) in rows
                if submissions_by_acc[acc].period_of_report == latest_period
            ]
            if not period_rows:
                for c in asset_cusips:
                    missing_cusips.append(c)
                continue

            matched_count += 1
            for c in asset_cusips:
                covered_cusips.append(c)

            # Group by manager CIK: CIK -> list of (accession, value, cusip)
            manager_accessions: dict[str, list[tuple[str, Decimal, str]]] = defaultdict(list)
            for cusip, cik, acc, val in period_rows:
                manager_accessions[cik].append((acc, val, cusip))

            # For each manager, determine operational accession and total value for that accession
            manager_summaries = []
            for cik, acc_list in manager_accessions.items():
                # All accessions for this manager in this asset & period
                distinct_accessions = sorted(set(acc for acc, _, _ in acc_list))
                is_amendment = any(
                    submissions_by_acc[acc].form == "13F-HR/A"
                    or (acc in coverpage_by_acc and coverpage_by_acc[acc].is_amendment)
                    for acc in distinct_accessions
                )

                # Sum value by accession
                value_by_acc: dict[str, Decimal] = defaultdict(Decimal)
                cusip_for_acc: dict[str, str] = {}
                for acc, val, cusip in acc_list:
                    value_by_acc[acc] += val
                    cusip_for_acc[acc] = cusip

                # Operational accession is the most recent by (filing_date, accession)
                operational_acc = max(
                    distinct_accessions,
                    key=lambda acc: (submissions_by_acc[acc].filing_date, acc),
                )
                op_sub = submissions_by_acc[operational_acc]
                op_cover = coverpage_by_acc.get(operational_acc)
                manager_name = op_cover.manager_name if op_cover else f"Manager {cik}"
                op_value = value_by_acc[operational_acc]
                cusip = cusip_for_acc[operational_acc]

                if max_filing_date is None or op_sub.filing_date > max_filing_date:
                    max_filing_date = op_sub.filing_date

                manager_summaries.append(
                    {
                        "cik": cik,
                        "manager_name": manager_name,
                        "operational_acc": operational_acc,
                        "form": op_sub.form,
                        "filing_date": op_sub.filing_date,
                        "report_period": op_sub.period_of_report,
                        "value_as_filed": op_value,
                        "is_amendment": is_amendment,
                        "accession_lineage": tuple(distinct_accessions),
                        "cusip": cusip,
                        "asset_id": asset_id,
                    }
                )

            # Sort managers for selection:
            # - value_as_filed descending
            # - manager_cik ascending
            # - accession ascending
            manager_summaries.sort(
                key=lambda item: (-item["value_as_filed"], item["cik"], item["operational_acc"])
            )

            # Assign rank and is_selected (top max_managers_per_asset)
            for index, item in enumerate(manager_summaries, start=1):
                is_selected = index <= max_managers_per_asset
                rank = index if is_selected else None

                if is_selected:
                    selected_count += 1
                else:
                    unselected_count += 1

                cand_id = candidate_id(
                    dataset_sha256=dataset_sha256,
                    asset_id=item["asset_id"],
                    cusip=item["cusip"],
                    manager_cik=item["cik"],
                    accession=item["operational_acc"],
                    form=item["form"],
                    report_period=item["report_period"],
                    schema_version=SEC_13F_MANAGER_UNIVERSE_SCHEMA_VERSION,
                )

                candidate = Sec13FManagerCandidate(
                    candidate_id=cand_id,
                    dataset_revision_id=dataset_revision_id,
                    asset_id=item["asset_id"],
                    cusip=item["cusip"],
                    manager_cik=item["cik"],
                    manager_name=item["manager_name"],
                    accession=item["operational_acc"],
                    form=item["form"],
                    filing_date=item["filing_date"],
                    report_period=item["report_period"],
                    value_as_filed=item["value_as_filed"],
                    value_unit="usd_thousands_as_filed",
                    is_amendment=item["is_amendment"],
                    is_selected=is_selected,
                    selection_rank=rank,
                    accession_lineage=item["accession_lineage"],
                )
                all_candidates.append(candidate)

        return (
            tuple(all_candidates),
            eligible_count,
            matched_count,
            selected_count,
            unselected_count,
            tuple(sorted(set(covered_cusips))),
            tuple(sorted(set(missing_cusips))),
            max_filing_date,
        )
