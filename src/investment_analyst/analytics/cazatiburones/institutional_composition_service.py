"""Read-only projection over persisted semantic 13F artifacts."""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.institutional_composition_engine import resolve
from investment_analyst.analytics.cazatiburones.institutional_composition_models import (
    InstitutionalCompositionCandidate,
    InstitutionalCompositionResult,
)
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError


class InstitutionalCompositionService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(
        self, *, manager_cik: str, known_at: datetime
    ) -> tuple[InstitutionalCompositionResult, ...]:
        if not self._storage.read_only:
            raise StorageError("institutional composition query requires read-only storage")
        normalized_manager_cik = normalize_cik(manager_cik)
        periods: defaultdict[date | None, list[InstitutionalCompositionCandidate]] = defaultdict(
            list
        )
        for record in self._storage.raw_records.list(
            source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
            schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
            available_to=known_at,
        ):
            item = semantics_from_raw_record(record)
            if item.manager_cik != normalized_manager_cik:
                continue
            periods[item.report_period].append(
                InstitutionalCompositionCandidate(
                    artifact_id=item.artifact_id,
                    accession=item.accession,
                    manager_cik=item.manager_cik,
                    report_period=item.report_period,
                    available_at=item.available_at,
                    is_amendment=item.is_amendment,
                    amendment_number=item.amendment_number,
                    amendment_type=item.amendment_type,
                    declared_entry_total=item.declared_entry_total,
                    declared_value_total=item.declared_value_total,
                    observed_entry_total=len(item.rows) if item.rows else None,
                    observed_value_total=(
                        sum((row.value_as_reported for row in item.rows), Decimal("0"))
                        if item.rows
                        else None
                    ),
                )
            )
        return tuple(
            resolve(
                manager_cik=normalized_manager_cik,
                report_period=period,
                known_at=known_at,
                candidates=tuple(candidates),
            )
            for period, candidates in sorted(
                periods.items(), key=lambda item: (item[0] is None, item[0])
            )
        )
