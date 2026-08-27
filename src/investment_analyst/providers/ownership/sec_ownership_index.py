"""Section 16 selection from persisted Submissions."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from investment_analyst.evidence.sec_ownership.models import OWNERSHIP_FORMS


@dataclass(frozen=True, slots=True)
class OwnershipFiling:
    accession: str
    filing_date: date
    report_date: date
    accepted_at: datetime
    form: str
    primary_document: str


def ownership_filings(record, configuration) -> tuple[OwnershipFiling, ...]:
    if (
        record.asset_id != configuration.asset_id
        or record.source.source_id != configuration.submissions_source_id
    ):
        raise ValueError("invalid submissions issuer")
    try:
        recent = record.payload["document"]["filings"]["recent"]
        rows = zip(
            recent["accessionNumber"],
            recent["filingDate"],
            recent["reportDate"],
            recent["acceptanceDateTime"],
            recent["form"],
            recent["primaryDocument"],
            strict=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("malformed submissions") from error
    results = []
    for accession, filing_date, report_date, accepted, form, document in rows:
        if form not in OWNERSHIP_FORMS:
            continue
        try:
            parsed = datetime.fromisoformat(accepted.replace("Z", "+00:00")).astimezone(UTC)
            results.append(
                OwnershipFiling(
                    accession,
                    date.fromisoformat(filing_date),
                    date.fromisoformat(report_date),
                    parsed,
                    form,
                    document,
                )
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid ownership filing") from error
    return tuple(sorted(results, key=lambda item: (item.accepted_at, item.accession)))
