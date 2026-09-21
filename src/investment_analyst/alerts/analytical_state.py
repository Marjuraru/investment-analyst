"""Append-only persistence and lifecycle for deterministic analytical candidates."""

import hashlib
import json
import threading
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.alerts.analytical_models import (
    AnalyticalScreeningDomain,
    AnalyticalScreeningResult,
)
from investment_analyst.application.bounded_journal import (
    BoundedOperationalJournal,
    atomic_write,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_CANDIDATE_NAMESPACE = UUID("64e57ce7-7bb4-5126-a4a4-a6f41e073ebf")
_TRANSITION_NAMESPACE = UUID("11cf67fb-4347-5a71-91f9-1a4bd27c86d1")
_MAX_STATE_RECORDS = 250_000


class AnalyticalMonitorReceiptStatus(StrEnum):
    """Durable outcome of observing one completed scheduled attempt."""

    SCREENED = "screened"
    SKIPPED = "skipped"


class AnalyticalCandidateStatus(StrEnum):
    """Local lifecycle of one evidence-backed candidate for human review."""

    NEW = "new"
    SEEN = "seen"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    SILENCED = "silenced"


class AnalyticalMonitorReceipt(ContractModel):
    """Exactly-once receipt for one scheduler-attempt observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["analytical-monitor-receipt-v1"] = "analytical-monitor-receipt-v1"
    attempt_id: UUID
    job_id: NonEmptyStr
    asset_id: NonEmptyStr | None = None
    status: AnalyticalMonitorReceiptStatus
    reason: NonEmptyStr
    processed_at: UTCDateTime
    result_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_receipt(self) -> "AnalyticalMonitorReceipt":
        """Keep status, result references, and deterministic ordering aligned."""
        if self.result_ids != tuple(sorted(set(self.result_ids), key=str)):
            raise ValueError("analytical monitor result_ids must be unique and sorted")
        if self.status is AnalyticalMonitorReceiptStatus.SCREENED and not self.result_ids:
            raise ValueError("screened analytical attempts must reference results")
        if self.status is AnalyticalMonitorReceiptStatus.SKIPPED and self.result_ids:
            raise ValueError("skipped analytical attempts cannot reference results")
        return self

    def semantic_fingerprint(self) -> str:
        """Hash immutable receipt semantics."""
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON primitives."""
        return self.model_dump(mode="json")


class AnalyticalCandidateEvent(ContractModel):
    """One deduplicated candidate created after confirmations and cooldown."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["analytical-candidate-event-v1"] = "analytical-candidate-event-v1"
    candidate_id: UUID
    activation_result_id: UUID
    rule_id: NonEmptyStr
    rule_version: NonEmptyStr
    rule_fingerprint: NonEmptyStr
    asset_id: NonEmptyStr
    domain: AnalyticalScreeningDomain
    source_id: NonEmptyStr
    as_of: UTCDateTime
    activated_at: UTCDateTime
    cooldown_until: UTCDateTime
    confirmations: int = Field(ge=1, le=20)
    status: AnalyticalCandidateStatus = AnalyticalCandidateStatus.NEW

    @model_validator(mode="after")
    def validate_event(self) -> "AnalyticalCandidateEvent":
        """Verify deterministic identity and bounded lifecycle time."""
        if self.candidate_id != analytical_candidate_id(self.activation_result_id):
            raise ValueError("analytical candidate_id is not deterministic")
        if self.cooldown_until < self.activated_at:
            raise ValueError("candidate cooldown cannot predate activation")
        return self

    def semantic_fingerprint(self) -> str:
        """Hash immutable candidate semantics independently from inbox status."""
        payload = self.model_dump(mode="json", exclude={"status"})
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON primitives."""
        return self.model_dump(mode="json")


class AnalyticalCandidateTransition(ContractModel):
    """Append-only audit record for a user or evidence lifecycle change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-candidate-transition-v1"] = (
        "analytical-candidate-transition-v1"
    )
    transition_id: UUID
    candidate_id: UUID
    from_status: AnalyticalCandidateStatus
    to_status: AnalyticalCandidateStatus
    recorded_at: UTCDateTime
    actor: Literal["local_user", "system_evidence"]

    @model_validator(mode="after")
    def validate_transition(self) -> "AnalyticalCandidateTransition":
        """Reject no-op transitions and verify their stable identity."""
        if self.from_status is self.to_status:
            raise ValueError("candidate transitions must change status")
        expected = analytical_candidate_transition_id(
            self.candidate_id,
            self.from_status,
            self.to_status,
            self.recorded_at,
            self.actor,
        )
        if self.transition_id != expected:
            raise ValueError("analytical candidate transition_id is not deterministic")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact audit primitives."""
        return self.model_dump(mode="json")


class AnalyticalScreeningState(ContractModel):
    """Complete recoverable state for the low-consumption analytical monitor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-screening-state-v1"] = "analytical-screening-state-v1"
    results: tuple[AnalyticalScreeningResult, ...] = Field(
        default=(), max_length=_MAX_STATE_RECORDS
    )
    candidates: tuple[AnalyticalCandidateEvent, ...] = Field(
        default=(), max_length=_MAX_STATE_RECORDS
    )
    transitions: tuple[AnalyticalCandidateTransition, ...] = Field(
        default=(), max_length=_MAX_STATE_RECORDS
    )
    receipts: tuple[AnalyticalMonitorReceipt, ...] = Field(
        default=(), max_length=_MAX_STATE_RECORDS
    )

    @model_validator(mode="after")
    def validate_history(self) -> "AnalyticalScreeningState":
        """Require unique identities, valid references, and sequential transitions."""
        result_ids = tuple(item.result_id for item in self.results)
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        transition_ids = tuple(item.transition_id for item in self.transitions)
        receipt_ids = tuple(item.attempt_id for item in self.receipts)
        for values, message in (
            (result_ids, "analytical results contain duplicate identities"),
            (candidate_ids, "analytical candidates contain duplicate identities"),
            (transition_ids, "candidate transitions contain duplicate identities"),
            (receipt_ids, "analytical receipts contain duplicate attempt identities"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(message)
        expected_orderings = (
            (
                tuple((item.known_at, str(item.result_id)) for item in self.results),
                "analytical results are not deterministically ordered",
            ),
            (
                tuple((item.activated_at, str(item.candidate_id)) for item in self.candidates),
                "analytical candidates are not deterministically ordered",
            ),
            (
                tuple((item.recorded_at, str(item.transition_id)) for item in self.transitions),
                "candidate transitions are not deterministically ordered",
            ),
            (
                tuple((item.processed_at, str(item.attempt_id)) for item in self.receipts),
                "analytical receipts are not deterministically ordered",
            ),
        )
        for ordering, message in expected_orderings:
            if ordering != tuple(sorted(ordering)):
                raise ValueError(message)
        known_results = set(result_ids)
        if any(item.activation_result_id not in known_results for item in self.candidates):
            raise ValueError("analytical candidate references an unknown result")
        if any(
            result_id not in known_results
            for receipt in self.receipts
            for result_id in receipt.result_ids
        ):
            raise ValueError("analytical receipt references an unknown result")
        candidates_by_id = {item.candidate_id: item for item in self.candidates}
        if any(item.candidate_id not in candidates_by_id for item in self.transitions):
            raise ValueError("candidate transition references an unknown candidate")
        projected = {item.candidate_id: AnalyticalCandidateStatus.NEW for item in self.candidates}
        for transition in self.transitions:
            if projected[transition.candidate_id] is not transition.from_status:
                raise ValueError("candidate transition history is not sequential")
            projected[transition.candidate_id] = transition.to_status
        if any(
            candidates_by_id[candidate_id].status is not status
            for candidate_id, status in projected.items()
        ):
            raise ValueError("candidate status differs from its transition history")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the complete recoverable monitor document."""
        return {
            "schema_version": self.schema_version,
            "results": [item.to_json_dict() for item in self.results],
            "candidates": [item.to_json_dict() for item in self.candidates],
            "transitions": [item.to_json_dict() for item in self.transitions],
            "receipts": [item.to_json_dict() for item in self.receipts],
        }


class AnalyticalCandidateInboxItem(ContractModel):
    """Candidate event joined to its exact activation result for review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: AnalyticalCandidateEvent
    result: AnalyticalScreeningResult


class AnalyticalCandidateInbox(ContractModel):
    """Bounded newest-first analytical-candidate response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-candidate-inbox-v1"] = "analytical-candidate-inbox-v1"
    silent_mode: Literal[True] = True
    total: int = Field(ge=0)
    items: tuple[AnalyticalCandidateInboxItem, ...] = Field(max_length=200)

    def to_json_dict(self) -> dict[str, object]:
        """Return candidates with their transparent condition evidence."""
        return self.model_dump(mode="json")


class AnalyticalCandidateInboxStatus(ContractModel):
    """Compact monitor status for the main overview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["analytical-candidate-inbox-status-v1"] = (
        "analytical-candidate-inbox-status-v1"
    )
    enabled: Literal[True] = True
    silent_mode: Literal[True] = True
    result_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    processed_attempt_count: int = Field(ge=0)
    latest_candidate_at: UTCDateTime | None = None

    def to_json_dict(self) -> dict[str, object]:
        """Return compact counts without loading analytical evidence."""
        return self.model_dump(mode="json")


class AnalyticalRecordOutcome(ContractModel):
    """Counts produced by one atomic attempt observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_created: bool
    results_created: int = Field(ge=0)
    candidates_created: int = Field(ge=0)
    candidates_resolved: int = Field(ge=0)


class AnalyticalScreeningReconciliation:
    """One validated analytical-state snapshot reused by one startup cycle."""

    def __init__(
        self, store: "AnalyticalScreeningStateStore", state: AnalyticalScreeningState
    ) -> None:
        self._store = store
        self._state = state
        self._attempt_ids = {item.attempt_id for item in state.receipts}

    def contains_attempt(self, attempt_id: UUID) -> bool:
        return attempt_id in self._attempt_ids

    def record_attempt(
        self,
        receipt: AnalyticalMonitorReceipt,
        results: tuple[AnalyticalScreeningResult, ...],
    ) -> AnalyticalRecordOutcome:
        with self._store._lock:
            outcome, self._state = self._store._record_attempt(self._state, receipt, results)
            self._attempt_ids.add(receipt.attempt_id)
            return outcome


def _reduce_analytical_screening_records(
    records: Sequence[dict[str, object]],
) -> Sequence[dict[str, object]]:
    results_by_id: dict[str, dict[str, object]] = {}
    candidates_by_id: dict[str, dict[str, object]] = {}
    transitions_by_id: dict[str, dict[str, object]] = {}
    receipts_by_id: dict[str, dict[str, object]] = {}

    for record in records:
        collection = record.get("collection")
        item = record.get("item")
        if not isinstance(item, dict):
            continue
        if collection == "result":
            rid = str(item.get("result_id", ""))
            results_by_id[rid] = record
        elif collection == "candidate":
            cid = str(item.get("candidate_id", ""))
            candidates_by_id[cid] = record
        elif collection == "transition":
            tid = str(item.get("transition_id", ""))
            transitions_by_id[tid] = record
        elif collection == "receipt":
            aid = str(item.get("attempt_id", ""))
            receipts_by_id[aid] = record

    def _result_key(rec: dict[str, object]) -> tuple[str, str]:
        it = cast(dict[str, object], rec.get("item", {}))
        return (str(it.get("known_at", "")), str(it.get("result_id", "")))

    def _candidate_key(rec: dict[str, object]) -> tuple[str, str]:
        it = cast(dict[str, object], rec.get("item", {}))
        return (str(it.get("activated_at", "")), str(it.get("candidate_id", "")))

    def _transition_key(rec: dict[str, object]) -> tuple[str, str]:
        it = cast(dict[str, object], rec.get("item", {}))
        return (str(it.get("recorded_at", "")), str(it.get("transition_id", "")))

    def _receipt_key(rec: dict[str, object]) -> tuple[str, str]:
        it = cast(dict[str, object], rec.get("item", {}))
        return (str(it.get("processed_at", "")), str(it.get("attempt_id", "")))

    sorted_results = sorted(results_by_id.values(), key=_result_key)
    sorted_candidates = sorted(candidates_by_id.values(), key=_candidate_key)
    sorted_transitions = sorted(transitions_by_id.values(), key=_transition_key)
    sorted_receipts = sorted(receipts_by_id.values(), key=_receipt_key)

    return tuple(sorted_results + sorted_candidates + sorted_transitions + sorted_receipts)


class AnalyticalScreeningStateStore:
    """Atomically persist results, receipts, candidates, and transitions via bounded journal."""

    def __init__(
        self,
        path: Path,
        *,
        journal_dir: Path | None = None,
        max_segment_bytes: int = 512 * 1024,
        max_segment_records: int = 5_000,
    ) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._journal_dir = (
            journal_dir.expanduser().resolve(strict=False)
            if journal_dir is not None
            else self._path.parent / f"{self._path.stem}_journal"
        )
        self._lock = threading.RLock()
        self._cached_state: AnalyticalScreeningState | None = None
        self._cached_attempt_ids: set[UUID] | None = None
        self._journal = BoundedOperationalJournal(
            directory=self._journal_dir,
            state_root=self._path.parent,
            max_segment_bytes=max_segment_bytes,
            max_segment_records=max_segment_records,
            journal_id="analytical-screening",
            reducer=_reduce_analytical_screening_records,
        )
        marker_file = self._journal_dir / ".legacy_v1_preserved"
        if marker_file.is_file() or (self._path.is_file() and not self._journal.has_data()):
            self._is_preexisting_legacy = True
        else:
            self._is_preexisting_legacy = False

    @property
    def journal_dir(self) -> Path:
        """Return the directory containing journal files."""
        return self._journal_dir

    def persisted_signatures(self) -> tuple[tuple[str, bytes], ...]:
        """Return filenames and byte contents of all files in the journal directory."""
        if not self._journal_dir.exists():
            return ()
        return tuple(
            (p.name, p.read_bytes()) for p in sorted(self._journal_dir.rglob("*")) if p.is_file()
        )

    def _sync_path(self, state: AnalyticalScreeningState) -> None:
        """Keep self._path synchronized when not a preserved legacy file."""
        if not self._is_preexisting_legacy:
            try:
                payload = (
                    json.dumps(
                        state.to_json_dict(),
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
                atomic_write(self._path, payload)
            except OSError as error:
                raise AaplOperationalStateError(
                    "analytical screening state could not be written"
                ) from error

    def load(self) -> AnalyticalScreeningState:
        """Load valid state without creating a missing file."""
        with self._lock:
            has_journal = self._journal.has_data()
            has_legacy = self._path.exists()

            legacy_state: AnalyticalScreeningState | None = None
            if has_legacy:
                try:
                    legacy_state = AnalyticalScreeningState.model_validate_json(
                        self._path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    self._cached_state = None
                    self._cached_attempt_ids = None
                    raise AaplOperationalStateError(
                        "analytical screening state is malformed or unreadable"
                    ) from error

            if self._cached_state is not None:
                return self._cached_state

            if not has_journal and not has_legacy:
                self._cached_attempt_ids = set()
                return AnalyticalScreeningState()

            legacy_results: list[AnalyticalScreeningResult] = []
            legacy_candidates: list[AnalyticalCandidateEvent] = []
            legacy_transitions: list[AnalyticalCandidateTransition] = []
            legacy_receipts: list[AnalyticalMonitorReceipt] = []
            if has_legacy and legacy_state is not None:
                if self._is_preexisting_legacy:
                    if not self._journal.has_legacy_v1_folded():
                        legacy_results = list(legacy_state.results)
                        legacy_candidates = list(legacy_state.candidates)
                        legacy_transitions = list(legacy_state.transitions)
                        legacy_receipts = list(legacy_state.receipts)
                else:
                    legacy_results = list(legacy_state.results)
                    legacy_candidates = list(legacy_state.candidates)
                    legacy_transitions = list(legacy_state.transitions)
                    legacy_receipts = list(legacy_state.receipts)

            try:
                journal_records = self._journal.read_entries()
            except AaplOperationalStateError as error:
                raise AaplOperationalStateError(
                    "analytical screening state is malformed or unreadable"
                ) from error

            journal_results: list[AnalyticalScreeningResult] = []
            journal_candidates: list[AnalyticalCandidateEvent] = []
            journal_transitions: list[AnalyticalCandidateTransition] = []
            journal_receipts: list[AnalyticalMonitorReceipt] = []

            for record in journal_records:
                if not isinstance(record, dict):
                    raise AaplOperationalStateError(
                        "analytical screening state is malformed or unreadable"
                    )
                collection = record.get("collection")
                item = record.get("item")
                if not isinstance(item, dict):
                    raise AaplOperationalStateError(
                        "analytical screening state is malformed or unreadable"
                    )
                try:
                    if collection == "result":
                        journal_results.append(AnalyticalScreeningResult.model_validate(item))
                    elif collection == "candidate":
                        journal_candidates.append(AnalyticalCandidateEvent.model_validate(item))
                    elif collection == "transition":
                        journal_transitions.append(
                            AnalyticalCandidateTransition.model_validate(item)
                        )
                    elif collection == "receipt":
                        journal_receipts.append(AnalyticalMonitorReceipt.model_validate(item))
                    else:
                        raise AaplOperationalStateError(
                            "analytical screening state is malformed or unreadable"
                        )
                except (ValueError, TypeError) as error:
                    raise AaplOperationalStateError(
                        "analytical screening state is malformed or unreadable"
                    ) from error

            if not self._is_preexisting_legacy and legacy_state is not None and has_legacy:
                allowed_receipt_ids = {r.attempt_id for r in legacy_state.receipts}
                allowed_result_ids = {r.result_id for r in legacy_state.results}
                allowed_candidate_ids = {c.candidate_id for c in legacy_state.candidates}
                journal_receipts = [
                    r for r in journal_receipts if r.attempt_id in allowed_receipt_ids
                ]
                journal_results = [r for r in journal_results if r.result_id in allowed_result_ids]
                journal_candidates = [
                    c for c in journal_candidates if c.candidate_id in allowed_candidate_ids
                ]

            by_result: dict[UUID, AnalyticalScreeningResult] = {
                item.result_id: item for item in legacy_results
            }
            for r in journal_results:
                existing_r = by_result.get(r.result_id)
                if existing_r is not None:
                    if existing_r.semantic_fingerprint() != r.semantic_fingerprint():
                        raise AaplOperationalStateError(
                            "analytical recomputation changed deterministic semantics"
                        )
                else:
                    by_result[r.result_id] = r

            by_candidate: dict[UUID, AnalyticalCandidateEvent] = {
                item.candidate_id: item for item in legacy_candidates
            }
            for c in journal_candidates:
                existing_c = by_candidate.get(c.candidate_id)
                if existing_c is not None:
                    if existing_c.semantic_fingerprint() != c.semantic_fingerprint():
                        raise AaplOperationalStateError(
                            "candidate recomputation changed deterministic semantics"
                        )
                else:
                    by_candidate[c.candidate_id] = c

            by_transition: dict[UUID, AnalyticalCandidateTransition] = {
                item.transition_id: item for item in legacy_transitions
            }
            for t in journal_transitions:
                by_transition[t.transition_id] = t

            by_receipt: dict[UUID, AnalyticalMonitorReceipt] = {
                item.attempt_id: item for item in legacy_receipts
            }
            for rc in journal_receipts:
                existing_rc = by_receipt.get(rc.attempt_id)
                if existing_rc is not None:
                    if existing_rc.semantic_fingerprint() != rc.semantic_fingerprint():
                        raise AaplOperationalStateError(
                            "analytical attempt replay changed receipt semantics"
                        )
                else:
                    by_receipt[rc.attempt_id] = rc

            sorted_transitions = tuple(
                sorted(
                    by_transition.values(),
                    key=lambda item: (item.recorded_at, str(item.transition_id)),
                )
            )

            projected = {
                item.candidate_id: AnalyticalCandidateStatus.NEW for item in by_candidate.values()
            }
            for t in sorted_transitions:
                projected[t.candidate_id] = t.to_status

            updated_candidates = {
                cid: cand.model_copy(update={"status": projected.get(cid, cand.status)})
                for cid, cand in by_candidate.items()
            }

            sorted_results = tuple(
                sorted(
                    by_result.values(),
                    key=lambda item: (item.known_at, str(item.result_id)),
                )
            )
            sorted_candidates = tuple(
                sorted(
                    updated_candidates.values(),
                    key=lambda item: (item.activated_at, str(item.candidate_id)),
                )
            )
            sorted_receipts = tuple(
                sorted(
                    by_receipt.values(),
                    key=lambda item: (item.processed_at, str(item.attempt_id)),
                )
            )

            try:
                state = AnalyticalScreeningState(
                    results=sorted_results,
                    candidates=sorted_candidates,
                    transitions=sorted_transitions,
                    receipts=sorted_receipts,
                )
            except ValueError as error:
                raise AaplOperationalStateError(
                    "analytical screening state is malformed or unreadable"
                ) from error

            self._cached_state = state
            self._cached_attempt_ids = {item.attempt_id for item in sorted_receipts}
            return state

    def contains_attempt(self, attempt_id: UUID) -> bool:
        """Return whether an observation receipt already makes replay unnecessary."""
        with self._lock:
            if self._cached_attempt_ids is None:
                self.load()
            assert self._cached_attempt_ids is not None
            return attempt_id in self._cached_attempt_ids

    def reconciliation(self) -> AnalyticalScreeningReconciliation:
        """Load and validate the document once for one ordered startup replay."""
        return AnalyticalScreeningReconciliation(self, self.load())

    def _ensure_legacy_v1_folded(self) -> None:
        if self._is_preexisting_legacy:
            marker_file = self._journal_dir / ".legacy_v1_preserved"
            if not marker_file.is_file() and self._journal_dir.is_dir():
                with suppress(OSError):
                    marker_file.touch()
        if self._path.exists() and not self._journal.has_legacy_v1_folded():
            try:
                legacy_state = AnalyticalScreeningState.model_validate_json(
                    self._path.read_text(encoding="utf-8")
                )
                legacy_records = (
                    [
                        {"collection": "result", "item": item.to_json_dict()}
                        for item in legacy_state.results
                    ]
                    + [
                        {"collection": "candidate", "item": item.to_json_dict()}
                        for item in legacy_state.candidates
                    ]
                    + [
                        {"collection": "transition", "item": item.to_json_dict()}
                        for item in legacy_state.transitions
                    ]
                    + [
                        {"collection": "receipt", "item": item.to_json_dict()}
                        for item in legacy_state.receipts
                    ]
                )
                if legacy_records:
                    self._journal.initialize_snapshot_with_legacy(legacy_records)
                else:
                    self._journal.mark_legacy_v1_folded()
                if self._is_preexisting_legacy:
                    marker_file = self._journal_dir / ".legacy_v1_preserved"
                    with suppress(OSError):
                        marker_file.touch()
            except (OSError, UnicodeError, ValueError) as error:
                raise AaplOperationalStateError(
                    "analytical screening state is malformed or unreadable"
                ) from error

        if not self._path.exists():
            try:
                stub = (
                    b'{"candidates":[],"receipts":[],"results":[],'
                    b'"schema_version":"analytical-screening-state-v1","transitions":[]}\n'
                )
                atomic_write(self._path, stub)
                self._journal.mark_legacy_v1_folded()
            except OSError as error:
                raise AaplOperationalStateError(
                    "analytical screening state could not be written"
                ) from error

    def record_attempt(
        self,
        receipt: AnalyticalMonitorReceipt,
        results: tuple[AnalyticalScreeningResult, ...],
    ) -> AnalyticalRecordOutcome:
        """Record one observed attempt exactly once and update candidate lifecycles."""
        with self._lock:
            outcome, _ = self._record_attempt(self.load(), receipt, results)
            return outcome

    def _record_attempt(
        self,
        state: AnalyticalScreeningState,
        receipt: AnalyticalMonitorReceipt,
        results: tuple[AnalyticalScreeningResult, ...],
    ) -> tuple[AnalyticalRecordOutcome, AnalyticalScreeningState]:
        if tuple(sorted((item.result_id for item in results), key=str)) != receipt.result_ids:
            raise ValueError("analytical receipt result_ids do not match supplied results")
        receipt_by_id = {item.attempt_id: item for item in state.receipts}
        existing_receipt = receipt_by_id.get(receipt.attempt_id)
        if existing_receipt is not None:
            if existing_receipt.semantic_fingerprint() != receipt.semantic_fingerprint():
                raise AaplOperationalStateError(
                    "analytical attempt replay changed receipt semantics"
                )
            return (
                AnalyticalRecordOutcome(
                    receipt_created=False,
                    results_created=0,
                    candidates_created=0,
                    candidates_resolved=0,
                ),
                state,
            )

        results_by_id = {item.result_id: item for item in state.results}
        created_results: list[AnalyticalScreeningResult] = []
        for result in results:
            existing_result = results_by_id.get(result.result_id)
            if existing_result is not None:
                if existing_result.semantic_fingerprint() != result.semantic_fingerprint():
                    raise AaplOperationalStateError(
                        "analytical recomputation changed deterministic semantics"
                    )
                continue
            results_by_id[result.result_id] = result
            created_results.append(result)

        candidates_by_id = {item.candidate_id: item for item in state.candidates}
        transitions = list(state.transitions)
        new_transitions: list[AnalyticalCandidateTransition] = []
        created_candidates: list[AnalyticalCandidateEvent] = []
        candidates_resolved = 0
        for result in sorted(results, key=lambda item: (item.known_at, str(item.result_id))):
            open_candidates = tuple(
                item
                for item in candidates_by_id.values()
                if self._same_stream(item, result)
                and item.status is not AnalyticalCandidateStatus.RESOLVED
            )
            if open_candidates:
                if result.retained is False:
                    for candidate in open_candidates:
                        transition = _candidate_transition(
                            candidate,
                            AnalyticalCandidateStatus.RESOLVED,
                            recorded_at=receipt.processed_at,
                            actor="system_evidence",
                        )
                        transitions.append(transition)
                        new_transitions.append(transition)
                        candidates_by_id[candidate.candidate_id] = candidate.model_copy(
                            update={"status": AnalyticalCandidateStatus.RESOLVED}
                        )
                        candidates_resolved += 1
                continue
            if not result.activated or result.as_of is None:
                continue
            confirmations = self._confirmation_count(
                tuple(results_by_id.values()),
                result,
            )
            if confirmations < result.rule.confirmations_required:
                continue
            previous = tuple(
                item for item in candidates_by_id.values() if self._same_stream(item, result)
            )
            if previous and receipt.processed_at < max(item.cooldown_until for item in previous):
                continue
            event = AnalyticalCandidateEvent(
                candidate_id=analytical_candidate_id(result.result_id),
                activation_result_id=result.result_id,
                rule_id=result.rule.rule_id,
                rule_version=result.rule.rule_version,
                rule_fingerprint=result.rule.semantic_fingerprint(),
                asset_id=result.asset_id,
                domain=result.rule.domain,
                source_id=result.source_id,
                as_of=result.as_of,
                activated_at=receipt.processed_at,
                cooldown_until=receipt.processed_at
                + timedelta(seconds=result.rule.cooldown_seconds),
                confirmations=confirmations,
            )
            existing_event = candidates_by_id.get(event.candidate_id)
            if existing_event is not None:
                if existing_event.semantic_fingerprint() != event.semantic_fingerprint():
                    raise AaplOperationalStateError(
                        "candidate recomputation changed deterministic semantics"
                    )
                continue
            candidates_by_id[event.candidate_id] = event
            created_candidates.append(event)

        receipt_by_id[receipt.attempt_id] = receipt
        snapshot = AnalyticalScreeningState(
            results=tuple(
                sorted(
                    results_by_id.values(),
                    key=lambda item: (item.known_at, str(item.result_id)),
                )
            ),
            candidates=tuple(
                sorted(
                    candidates_by_id.values(),
                    key=lambda item: (item.activated_at, str(item.candidate_id)),
                )
            ),
            transitions=tuple(
                sorted(
                    transitions,
                    key=lambda item: (item.recorded_at, str(item.transition_id)),
                )
            ),
            receipts=tuple(
                sorted(
                    receipt_by_id.values(),
                    key=lambda item: (item.processed_at, str(item.attempt_id)),
                )
            ),
        )

        self._ensure_legacy_v1_folded()
        for res in created_results:
            self._journal.append({"collection": "result", "item": res.to_json_dict()})
        for cand in created_candidates:
            self._journal.append({"collection": "candidate", "item": cand.to_json_dict()})
        for tr in new_transitions:
            self._journal.append({"collection": "transition", "item": tr.to_json_dict()})
        self._journal.append({"collection": "receipt", "item": receipt.to_json_dict()})

        self._cached_state = snapshot
        if self._cached_attempt_ids is not None:
            self._cached_attempt_ids.add(receipt.attempt_id)
        else:
            self._cached_attempt_ids = {item.attempt_id for item in snapshot.receipts}
        self._sync_path(snapshot)

        return (
            AnalyticalRecordOutcome(
                receipt_created=True,
                results_created=len(created_results),
                candidates_created=len(created_candidates),
                candidates_resolved=candidates_resolved,
            ),
            snapshot,
        )

    def transition(
        self,
        candidate_id: UUID,
        to_status: AnalyticalCandidateStatus,
        *,
        recorded_at: datetime,
    ) -> tuple[AnalyticalCandidateEvent, bool]:
        """Apply one idempotent allowed user transition with audit evidence."""
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        recorded_at = recorded_at.astimezone(UTC)
        if to_status is AnalyticalCandidateStatus.NEW:
            raise ValueError("a candidate cannot transition back to new")
        with self._lock:
            state = self.load()
            candidates = {item.candidate_id: item for item in state.candidates}
            current = candidates.get(candidate_id)
            if current is None:
                raise ValueError("analytical candidate does not exist")
            if current.status is to_status:
                return current, False
            allowed = {
                AnalyticalCandidateStatus.NEW: {
                    AnalyticalCandidateStatus.SEEN,
                    AnalyticalCandidateStatus.DISMISSED,
                    AnalyticalCandidateStatus.RESOLVED,
                    AnalyticalCandidateStatus.SILENCED,
                },
                AnalyticalCandidateStatus.SEEN: {
                    AnalyticalCandidateStatus.DISMISSED,
                    AnalyticalCandidateStatus.RESOLVED,
                    AnalyticalCandidateStatus.SILENCED,
                },
                AnalyticalCandidateStatus.DISMISSED: {
                    AnalyticalCandidateStatus.RESOLVED,
                },
                AnalyticalCandidateStatus.SILENCED: {
                    AnalyticalCandidateStatus.RESOLVED,
                },
                AnalyticalCandidateStatus.RESOLVED: set(),
            }
            if to_status not in allowed[current.status]:
                raise ValueError("analytical candidate transition is not allowed")
            transition = _candidate_transition(
                current,
                to_status,
                recorded_at=recorded_at,
                actor="local_user",
            )
            candidates[candidate_id] = current.model_copy(update={"status": to_status})
            snapshot = AnalyticalScreeningState(
                results=state.results,
                candidates=tuple(
                    sorted(
                        candidates.values(),
                        key=lambda item: (
                            item.activated_at,
                            str(item.candidate_id),
                        ),
                    )
                ),
                transitions=tuple(
                    sorted(
                        (*state.transitions, transition),
                        key=lambda item: (
                            item.recorded_at,
                            str(item.transition_id),
                        ),
                    )
                ),
                receipts=state.receipts,
            )
            self._ensure_legacy_v1_folded()
            self._journal.append({"collection": "transition", "item": transition.to_json_dict()})
            self._cached_state = snapshot
            self._sync_path(snapshot)
            return candidates[candidate_id], True

    def status(self) -> AnalyticalCandidateInboxStatus:
        """Return compact state counts."""
        state = self.load()
        return AnalyticalCandidateInboxStatus(
            result_count=len(state.results),
            candidate_count=len(state.candidates),
            new_count=sum(
                item.status is AnalyticalCandidateStatus.NEW for item in state.candidates
            ),
            processed_attempt_count=len(state.receipts),
            latest_candidate_at=(
                max(item.activated_at for item in state.candidates) if state.candidates else None
            ),
        )

    def inbox(self, *, limit: int = 50) -> AnalyticalCandidateInbox:
        """Return bounded candidates joined to their exact activation evidence."""
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("candidate inbox limit must be between 1 and 200")
        state = self.load()
        results = {item.result_id: item for item in state.results}
        ordered = tuple(
            sorted(
                state.candidates,
                key=lambda item: (item.activated_at, str(item.candidate_id)),
                reverse=True,
            )
        )
        return AnalyticalCandidateInbox(
            total=len(ordered),
            items=tuple(
                AnalyticalCandidateInboxItem(
                    event=event,
                    result=results[event.activation_result_id],
                )
                for event in ordered[:limit]
            ),
        )

    @staticmethod
    def _same_stream(
        candidate: AnalyticalCandidateEvent,
        result: AnalyticalScreeningResult,
    ) -> bool:
        return (
            candidate.rule_fingerprint == result.rule.semantic_fingerprint()
            and candidate.asset_id == result.asset_id
            and candidate.source_id == result.source_id
        )

    @staticmethod
    def _confirmation_count(
        results: tuple[AnalyticalScreeningResult, ...],
        current: AnalyticalScreeningResult,
    ) -> int:
        compatible = sorted(
            (
                item
                for item in results
                if item.rule.semantic_fingerprint() == current.rule.semantic_fingerprint()
                and item.asset_id == current.asset_id
                and item.source_id == current.source_id
                and item.known_at <= current.known_at
                and item.as_of is not None
            ),
            key=lambda item: (item.known_at, str(item.result_id)),
            reverse=True,
        )
        count = 0
        seen_periods: set[datetime] = set()
        for item in compatible:
            if item.as_of in seen_periods:
                continue
            seen_periods.add(item.as_of)
            if not item.matched:
                break
            count += 1
        return count

    def _write(self, state: AnalyticalScreeningState) -> None:
        self._cached_state = state
        self._cached_attempt_ids = {item.attempt_id for item in state.receipts}
        document = (
            json.dumps(
                state.to_json_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        atomic_write(self._path, document)


def analytical_candidate_id(result_id: UUID) -> UUID:
    """Return one stable candidate identity for an activation result."""
    return uuid5(_CANDIDATE_NAMESPACE, str(result_id))


def analytical_candidate_transition_id(
    candidate_id: UUID,
    from_status: AnalyticalCandidateStatus,
    to_status: AnalyticalCandidateStatus,
    recorded_at: datetime,
    actor: Literal["local_user", "system_evidence"],
) -> UUID:
    """Return one stable transition identity from its complete audit semantics."""
    payload = ":".join(
        (
            str(candidate_id),
            from_status.value,
            to_status.value,
            recorded_at.astimezone(UTC).isoformat(),
            actor,
        )
    )
    return uuid5(_TRANSITION_NAMESPACE, payload)


def _candidate_transition(
    candidate: AnalyticalCandidateEvent,
    to_status: AnalyticalCandidateStatus,
    *,
    recorded_at: datetime,
    actor: Literal["local_user", "system_evidence"],
) -> AnalyticalCandidateTransition:
    return AnalyticalCandidateTransition(
        transition_id=analytical_candidate_transition_id(
            candidate.candidate_id,
            candidate.status,
            to_status,
            recorded_at,
            actor,
        ),
        candidate_id=candidate.candidate_id,
        from_status=candidate.status,
        to_status=to_status,
        recorded_at=recorded_at,
        actor=actor,
    )


__all__ = [
    "AnalyticalCandidateEvent",
    "AnalyticalCandidateInbox",
    "AnalyticalCandidateInboxItem",
    "AnalyticalCandidateInboxStatus",
    "AnalyticalCandidateStatus",
    "AnalyticalCandidateTransition",
    "AnalyticalMonitorReceipt",
    "AnalyticalMonitorReceiptStatus",
    "AnalyticalRecordOutcome",
    "AnalyticalScreeningState",
    "AnalyticalScreeningStateStore",
    "analytical_candidate_id",
    "analytical_candidate_transition_id",
]
