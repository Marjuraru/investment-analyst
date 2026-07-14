"""Read-only point-in-time selection of normalized Apple SEC observations."""

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime
from uuid import UUID

from investment_analyst.core.models import DataFrequency, DataQuality, NormalizedObservation
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPeriodView,
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
    SecSelectedFundamentalFact,
    allowed_sec_fundamental_fields,
)
from investment_analyst.storage import LocalStorage

_ALLOWED_FIELDS = frozenset(allowed_sec_fundamental_fields())
_REQUIRED_RECORD_KEY_FIELDS = frozenset(
    {
        "accession_number",
        "taxonomy",
        "tag",
        "unit",
        "period",
        "companyfacts_record_id",
        "submissions_record_id",
    }
)
_OPTIONAL_RECORD_KEY_FIELDS = frozenset({"form", "fiscal_year", "fiscal_period"})


class SecFundamentalQueryError(RuntimeError):
    """Base error for local SEC fundamental queries."""


class MalformedSecFundamentalObservationError(SecFundamentalQueryError):
    """Raised when an apparent SEC observation has invalid audit metadata."""


class AmbiguousSecFundamentalRevisionError(SecFundamentalQueryError):
    """Raised when equally available revisions disagree semantically."""


class SecFundamentalTraceabilityError(SecFundamentalQueryError):
    """Raised when the selected in-memory view cannot be audited."""


class _ParsedRecordKey:
    """Validated audit metadata decoded from one canonical record key."""

    __slots__ = (
        "accession_number",
        "companyfacts_record_id",
        "fiscal_period",
        "fiscal_year",
        "form",
        "period",
        "submissions_record_id",
        "tag",
        "taxonomy",
        "unit",
    )

    def __init__(
        self,
        *,
        accession_number: str,
        taxonomy: str,
        tag: str,
        unit: str,
        period: date,
        companyfacts_record_id: UUID,
        submissions_record_id: UUID,
        form: str | None,
        fiscal_year: str | None,
        fiscal_period: str | None,
    ) -> None:
        self.accession_number = accession_number
        self.taxonomy = taxonomy
        self.tag = tag
        self.unit = unit
        self.period = period
        self.companyfacts_record_id = companyfacts_record_id
        self.submissions_record_id = submissions_record_id
        self.form = form
        self.fiscal_year = fiscal_year
        self.fiscal_period = fiscal_period


class SecAaplFundamentalPointInTimeService:
    """Build point-in-time period views without reading SEC RawRecords."""

    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(self, request: SecFundamentalQuery) -> SecFundamentalPointInTimeResult:
        """Select the latest publicly available revision for each field and period."""
        self._storage.require_open()
        observations = self._storage.observations.list(asset_id=ASSET_ID)
        candidates = self._eligible_candidates(observations, request)
        selected, superseded = self._resolve_revisions(candidates, request)
        periods = self._build_periods(selected, request)
        if request.limit is not None:
            periods = periods[-request.limit :]
        result = SecFundamentalPointInTimeResult(
            query=request,
            periods=tuple(periods),
            observations_examined=len(observations),
            observations_eligible=len(candidates),
            observations_selected=sum(len(period.facts) for period in periods),
            observations_superseded=sum(
                fact.superseded_count for period in periods for fact in period.facts
            ),
            periods_returned=len(periods),
            earliest_period_end=periods[0].period_end if periods else None,
            latest_period_end=periods[-1].period_end if periods else None,
            latest_period_complete=periods[-1].is_complete if periods else False,
            traceability_verified=True,
        )
        self._verify_result(result, candidates, superseded)
        return result

    def _eligible_candidates(
        self,
        observations: list[NormalizedObservation],
        request: SecFundamentalQuery,
    ) -> list[SecSelectedFundamentalFact]:
        candidates: list[SecSelectedFundamentalFact] = []
        for observation in observations:
            if not _appears_in_scope(observation, request):
                continue
            if observation.unit != "USD":
                raise MalformedSecFundamentalObservationError(
                    f"SEC observation {observation.observation_id} must use USD"
                )
            if observation.period_end is None:
                raise MalformedSecFundamentalObservationError(
                    f"SEC observation {observation.observation_id} lacks period_end"
                )
            if not _period_in_range(observation.period_end.date(), request):
                continue
            parsed = _parse_record_key(observation)
            candidates.append(_selected_fact(observation, parsed, superseded_count=0))
        return candidates

    def _resolve_revisions(
        self,
        candidates: list[SecSelectedFundamentalFact],
        request: SecFundamentalQuery,
    ) -> tuple[list[SecSelectedFundamentalFact], int]:
        groups: dict[tuple[str, DataFrequency, datetime], list[SecSelectedFundamentalFact]] = (
            defaultdict(list)
        )
        for fact in candidates:
            groups[(fact.field_name, fact.frequency, fact.period_end)].append(fact)

        selected: list[SecSelectedFundamentalFact] = []
        superseded_total = 0
        for key in sorted(groups, key=lambda item: (item[2], item[1].value, item[0])):
            revisions = _collapse_equal_availability(groups[key])
            revisions.sort(key=lambda fact: fact.available_at)
            current = revisions[-1]
            superseded_count = len(revisions) - 1
            superseded_total += superseded_count
            selected.append(current.model_copy(update={"superseded_count": superseded_count}))
        if any(fact.available_at > request.known_at for fact in selected):
            raise SecFundamentalTraceabilityError("a selected fact is not known at query time")
        return selected, superseded_total

    def _build_periods(
        self,
        selected: list[SecSelectedFundamentalFact],
        request: SecFundamentalQuery,
    ) -> list[SecFundamentalPeriodView]:
        grouped: dict[datetime, list[SecSelectedFundamentalFact]] = defaultdict(list)
        for fact in selected:
            grouped[fact.period_end].append(fact)

        all_fields = set(allowed_sec_fundamental_fields())
        periods: list[SecFundamentalPeriodView] = []
        for period_end in sorted(grouped):
            facts = tuple(sorted(grouped[period_end], key=lambda fact: fact.field_name))
            available = tuple(sorted(fact.field_name for fact in facts))
            missing = tuple(sorted(all_fields - set(available)))
            periods.append(
                SecFundamentalPeriodView(
                    period_end=period_end,
                    frequency=request.frequency,
                    facts=facts,
                    missing_fields=missing,
                    available_fields=available,
                    is_complete=not missing,
                    latest_available_at=max(fact.available_at for fact in facts),
                )
            )
        return periods

    def _verify_result(
        self,
        result: SecFundamentalPointInTimeResult,
        candidates: list[SecSelectedFundamentalFact],
        superseded_total: int,
    ) -> None:
        candidate_ids = {fact.observation_id for fact in candidates}
        for period in result.periods:
            for fact in period.facts:
                if fact.observation_id not in candidate_ids:
                    raise SecFundamentalTraceabilityError(
                        "selected fact is not part of the eligible observation set"
                    )
                if fact.source_id != COMPANYFACTS_SOURCE_ID:
                    raise SecFundamentalTraceabilityError("selected fact has the wrong source")
                if fact.available_at > result.query.known_at:
                    raise SecFundamentalTraceabilityError("selected fact is not point-in-time safe")
                if fact.period_end != period.period_end:
                    raise SecFundamentalTraceabilityError("selected fact period is inconsistent")
        if result.observations_superseded > superseded_total:
            raise SecFundamentalTraceabilityError("superseded count is inconsistent")


def _appears_in_scope(
    observation: NormalizedObservation,
    request: SecFundamentalQuery,
) -> bool:
    if observation.field_name not in _ALLOWED_FIELDS:
        return False
    if observation.source.source_id != COMPANYFACTS_SOURCE_ID:
        return False
    if observation.transformation_version != TRANSFORMATION_VERSION:
        return False
    if observation.quality is not DataQuality.VALID:
        return False
    if observation.frequency is not request.frequency:
        return False
    return observation.available_at <= request.known_at


def _period_in_range(period_end: date, request: SecFundamentalQuery) -> bool:
    if request.start_period_end is not None and period_end < request.start_period_end:
        return False
    return request.end_period_end is None or period_end <= request.end_period_end


def _parse_record_key(observation: NormalizedObservation) -> _ParsedRecordKey:
    record_key = observation.source.record_key
    if record_key is None:
        raise MalformedSecFundamentalObservationError("SEC observation record_key is missing")
    try:
        decoded = json.loads(record_key, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise MalformedSecFundamentalObservationError(
            "SEC observation record_key must be strict JSON"
        ) from error
    if not isinstance(decoded, Mapping) or not all(isinstance(key, str) for key in decoded):
        raise MalformedSecFundamentalObservationError("SEC record_key must be a JSON object")
    fields = frozenset(decoded)
    missing = _REQUIRED_RECORD_KEY_FIELDS - fields
    unknown = fields - _REQUIRED_RECORD_KEY_FIELDS - _OPTIONAL_RECORD_KEY_FIELDS
    if missing:
        names = ", ".join(sorted(missing))
        raise MalformedSecFundamentalObservationError(
            f"SEC record_key is missing required fields: {names}"
        )
    if unknown:
        names = ", ".join(sorted(unknown))
        raise MalformedSecFundamentalObservationError(
            f"SEC record_key contains unsupported fields: {names}"
        )

    companyfacts_record_id = _uuid_value(decoded, "companyfacts_record_id")
    if companyfacts_record_id != observation.raw_record_id:
        raise MalformedSecFundamentalObservationError(
            "companyfacts_record_id does not match observation.raw_record_id"
        )
    period = _date_value(decoded, "period")
    if observation.period_end is None or period != observation.period_end.date():
        raise MalformedSecFundamentalObservationError(
            "record_key period does not match observation.period_end"
        )
    unit = _string_value(decoded, "unit")
    if unit != observation.unit:
        raise MalformedSecFundamentalObservationError(
            "record_key unit does not match observation.unit"
        )
    return _ParsedRecordKey(
        accession_number=_string_value(decoded, "accession_number"),
        taxonomy=_string_value(decoded, "taxonomy"),
        tag=_string_value(decoded, "tag"),
        unit=unit,
        period=period,
        companyfacts_record_id=companyfacts_record_id,
        submissions_record_id=_uuid_value(decoded, "submissions_record_id"),
        form=_optional_string(decoded, "form"),
        fiscal_year=_optional_fiscal_year(decoded.get("fiscal_year")),
        fiscal_period=_optional_string(decoded, "fiscal_period"),
    )


def _selected_fact(
    observation: NormalizedObservation,
    parsed: _ParsedRecordKey,
    *,
    superseded_count: int,
) -> SecSelectedFundamentalFact:
    return SecSelectedFundamentalFact(
        observation_id=observation.observation_id,
        raw_record_id=observation.raw_record_id,
        field_name=observation.field_name,
        value=observation.value,
        unit=observation.unit,
        frequency=observation.frequency,
        period_start=observation.period_start,
        period_end=observation.period_end,
        available_at=observation.available_at,
        normalized_at=observation.normalized_at,
        accession_number=parsed.accession_number,
        taxonomy=parsed.taxonomy,
        tag=parsed.tag,
        form=parsed.form,
        fiscal_year=parsed.fiscal_year,
        fiscal_period=parsed.fiscal_period,
        source_id=observation.source.source_id,
        record_key=observation.source.record_key or "",
        superseded_count=superseded_count,
    )


def _collapse_equal_availability(
    revisions: list[SecSelectedFundamentalFact],
) -> list[SecSelectedFundamentalFact]:
    by_time: dict[datetime, list[SecSelectedFundamentalFact]] = defaultdict(list)
    for revision in revisions:
        by_time[revision.available_at].append(revision)

    collapsed: list[SecSelectedFundamentalFact] = []
    for available_at in sorted(by_time):
        tied = by_time[available_at]
        identities = {_semantic_identity(item) for item in tied}
        if len(identities) > 1:
            raise AmbiguousSecFundamentalRevisionError(
                "SEC revisions with equal available_at disagree semantically"
            )
        collapsed.append(tied[0])
    return collapsed


def _semantic_identity(fact: SecSelectedFundamentalFact) -> tuple[object, ...]:
    return (
        fact.field_name,
        fact.frequency,
        fact.period_start,
        fact.period_end,
        fact.value,
        fact.accession_number,
        fact.form,
        fact.fiscal_year,
        fact.fiscal_period,
        fact.taxonomy,
        fact.tag,
        fact.unit,
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _string_value(mapping: Mapping[str, object], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedSecFundamentalObservationError(
            f"SEC record_key field {name!r} must be a non-empty string"
        )
    return value.strip()


def _optional_string(mapping: Mapping[str, object], name: str) -> str | None:
    value = mapping.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MalformedSecFundamentalObservationError(
            f"SEC record_key field {name!r} must be a non-empty string when present"
        )
    return value.strip()


def _optional_fiscal_year(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MalformedSecFundamentalObservationError("fiscal_year must not be boolean")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise MalformedSecFundamentalObservationError(
        "fiscal_year must be a non-empty string or integer when present"
    )


def _uuid_value(mapping: Mapping[str, object], name: str) -> UUID:
    value = _string_value(mapping, name)
    try:
        return UUID(value)
    except ValueError as error:
        raise MalformedSecFundamentalObservationError(
            f"SEC record_key field {name!r} must be a UUID"
        ) from error


def _date_value(mapping: Mapping[str, object], name: str) -> date:
    value = _string_value(mapping, name)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise MalformedSecFundamentalObservationError(
            f"SEC record_key field {name!r} must be an ISO date"
        ) from error


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)
