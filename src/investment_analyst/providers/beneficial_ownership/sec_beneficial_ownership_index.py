"""Select supported Schedule 13D/13G filings from persisted Submissions."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from investment_analyst.evidence.sec_documents.models import BENEFICIAL_OWNERSHIP_FORMS


@dataclass(frozen=True, slots=True)
class BeneficialOwnershipFiling:
    accession: str
    filing_date: date
    report_date: date | None
    accepted_at: datetime
    form: str
    primary_document: str


def beneficial_ownership_filings(record, configuration) -> tuple[BeneficialOwnershipFiling, ...]:
    if (
        record.asset_id != configuration.asset_id
        or record.source.source_id != configuration.submissions_source_id
    ):
        raise ValueError("invalid submissions issuer")
    try:
        recent = record.payload["document"]["filings"]["recent"]
        accessions = recent["accessionNumber"]
        report_dates = recent.get("reportDate", (None,) * len(accessions))
        rows = zip(
            accessions,
            recent["filingDate"],
            report_dates,
            recent["acceptanceDateTime"],
            recent["form"],
            recent["primaryDocument"],
            strict=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("malformed submissions") from error
    filings: list[BeneficialOwnershipFiling] = []
    for accession, filing_date, report_date, accepted_at, form, primary_document in rows:
        if form not in BENEFICIAL_OWNERSHIP_FORMS:
            continue
        try:
            accepted = datetime.fromisoformat(accepted_at.replace("Z", "+00:00")).astimezone(UTC)
            filings.append(
                BeneficialOwnershipFiling(
                    accession=accession,
                    filing_date=date.fromisoformat(filing_date),
                    report_date=_report_date(report_date),
                    accepted_at=accepted,
                    form=form,
                    primary_document=primary_document,
                )
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid beneficial ownership filing") from error
    return tuple(sorted(filings, key=lambda item: (item.accepted_at, item.accession)))


def _report_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("report date must be a string")
    return date.fromisoformat(value)
