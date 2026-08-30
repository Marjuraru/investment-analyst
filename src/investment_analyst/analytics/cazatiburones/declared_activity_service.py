# ruff: noqa: E501
"""Read-only composition of declared ownership activity features."""

from datetime import date

from investment_analyst.analytics.cazatiburones.beneficial_ownership_change_engine import (
    calculate_beneficial_features,
)
from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityQueryResult,
)
from investment_analyst.analytics.cazatiburones.insider_activity_engine import (
    calculate_insider_features,
)
from investment_analyst.core.models.base import UTCDateTime
from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipStatement
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_ownership.models import OwnershipStatement
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.storage import StorageError
from investment_analyst.storage.local import LocalStorage


class DeclaredActivityService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(self, *, asset_id: str, known_at: UTCDateTime) -> DeclaredActivityQueryResult:
        if not self._storage.read_only:
            raise StorageError("declared activity query requires read-only storage")
        insider = OwnershipRepository(self._storage.raw_records).list(
            asset_id=asset_id, known_at=known_at
        )
        beneficial = BeneficialOwnershipRepository(self._storage.raw_records).list(
            asset_id=asset_id, known_at=known_at
        )
        _reject_unresolved_insider_amendments(insider)
        _reject_unresolved_beneficial_amendments(beneficial)
        return DeclaredActivityQueryResult(
            asset_id=asset_id,
            known_at=known_at,
            insider_features=calculate_insider_features(insider),
            beneficial_features=calculate_beneficial_features(beneficial),
            total_statements=len(insider) + len(beneficial),
        )


def _reject_unresolved_insider_amendments(statements: list[OwnershipStatement]) -> None:
    revisions: dict[tuple[str, str, str, date, str], set[bool]] = {}
    for statement in statements:
        for entry in statement.entries:
            if entry.kind != "transaction":
                continue
            key = (
                entry.owner_cik,
                entry.security_title,
                entry.table,
                entry.transaction_date or statement.period_of_report,
                statement.form.removesuffix("/A"),
            )
            revisions.setdefault(key, set()).add(statement.form.endswith("/A"))
    if any(len(forms) > 1 for forms in revisions.values()):
        raise ValueError("coexisting insider amendment and original require explicit resolution")


def _reject_unresolved_beneficial_amendments(
    statements: list[BeneficialOwnershipStatement],
) -> None:
    revisions: dict[tuple[str, str, date | None, str], set[bool]] = {}
    for statement in statements:
        if statement.reporting_person_cik is None:
            continue
        key = (
            statement.subject_cik,
            statement.reporting_person_cik,
            statement.event_date,
            statement.form.removesuffix("/A"),
        )
        revisions.setdefault(key, set()).add(statement.form.endswith("/A"))
    if any(len(forms) > 1 for forms in revisions.values()):
        raise ValueError("coexisting beneficial amendment and original require explicit resolution")
