"""Contract tests for the canonical, storage-free evidence-lineage representation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from investment_analyst.analytics import evidence_set as evidence_set_module
from investment_analyst.analytics.evidence_set import (
    SEGMENTATION_POLICY,
    EvidenceSegment,
    EvidenceSegmentationError,
    EvidenceSet,
    EvidenceSetConstructionError,
    EvidenceSetVerificationError,
    build_evidence_segments,
    build_evidence_set,
    canonical_lineage_hash,
    resolve_evidence_set,
    verify_evidence_set,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)

_ASSET = "crypto:btc-usd"
_SOURCE = "deribit:btc-perpetual:funding-rate-history"
_FIELD = "funding_interest_1h"
_START = datetime(2026, 6, 1, tzinfo=UTC)
_AVAILABLE = datetime(2026, 9, 1, tzinfo=UTC)


def _observation(
    offset_hours: int,
    *,
    frequency: DataFrequency = DataFrequency.HOUR_1,
    asset_id: str = _ASSET,
    source_id: str = _SOURCE,
    field_name: str = _FIELD,
    revision: str = "v1",
) -> NormalizedObservation:
    observed_at = _START + timedelta(hours=offset_hours)
    return NormalizedObservation(
        observation_id=uuid5(
            NAMESPACE_URL,
            f"obs:{revision}:{asset_id}:{source_id}:{field_name}:{observed_at.isoformat()}",
        ),
        raw_record_id=uuid5(NAMESPACE_URL, f"raw:{asset_id}:{source_id}:{observed_at.isoformat()}"),
        asset_id=asset_id,
        field_name=field_name,
        value=Decimal("0.000001"),
        unit="ratio",
        frequency=frequency,
        observed_at=observed_at,
        available_at=_AVAILABLE,
        normalized_at=_AVAILABLE + timedelta(minutes=1),
        source=SourceReference(
            source_id=source_id, record_key=observed_at.isoformat(), retrieved_at=_AVAILABLE
        ),
        quality=DataQuality.VALID,
        transformation_version="evidence-set-test-v1",
    )


def _series(count: int, **overrides: object) -> tuple[NormalizedObservation, ...]:
    return tuple(_observation(index, **overrides) for index in range(count))


def _identifiers(observations: tuple[NormalizedObservation, ...]) -> tuple[UUID, ...]:
    return tuple(item.observation_id for item in observations)


def test_builds_segments_and_sets_under_segmentation_v1_and_rejects_other_frequencies() -> None:
    """A1: hourly series build segments and sets; any other frequency fails closed."""
    series = _series(48)
    segments = build_evidence_segments(series)

    assert [segment.day for segment in segments] == [date(2026, 6, 1), date(2026, 6, 2)]
    assert all(segment.segmentation_policy == SEGMENTATION_POLICY for segment in segments)
    assert all(len(segment.observation_ids) == 24 for segment in segments)
    assert all(segment.segment_id.version == 8 for segment in segments)
    assert [segment.observation_ids for segment in segments] == [
        _identifiers(series[:24]),
        _identifiers(series[24:]),
    ]

    evidence = build_evidence_set(series[4:40], segments=segments)

    assert evidence.segmentation_policy == SEGMENTATION_POLICY
    assert evidence.segment_ids == tuple(segment.segment_id for segment in segments)
    assert evidence.head_offset == 4
    assert evidence.input_count == 36
    assert resolve_evidence_set(evidence, segments) == _identifiers(series[4:40])

    daily = tuple(_observation(index, frequency=DataFrequency.DAY_1) for index in range(3))
    with pytest.raises(EvidenceSegmentationError, match="hourly"):
        build_evidence_segments(daily)
    with pytest.raises(EvidenceSegmentationError, match="hourly"):
        build_evidence_set(daily, segments=segments)


def test_resolution_returns_the_exact_ordered_sequence_for_aligned_and_unaligned_windows() -> None:
    """A2: resolution is exact for aligned, unaligned, tail and inline-only windows."""
    series = _series(77)  # three complete UTC days plus a five-hour incomplete day
    segments = build_evidence_segments(series)
    assert len(segments) == 3

    windows = {
        "aligned": series[24:48],
        "unaligned": series[20:44],
        "crossing": series[30:54],
        "tail": series[30:53],
        "inline-only": series[72:77],
        "mixed": series[70:77],
        "whole": series,
    }
    for label, window in windows.items():
        evidence = build_evidence_set(window, segments=segments)
        resolved = resolve_evidence_set(evidence, segments)
        assert resolved == _identifiers(window), label
        assert len(resolved) == evidence.input_count, label
        assert len(evidence.inline_observation_ids) <= 23, label


def test_canonical_hash_is_independent_of_segmentation() -> None:
    """A3: the hash covers the ordered sequence only, never how it was segmented."""
    series = _series(77)
    segments = build_evidence_segments(series)
    window = series[70:77]
    identifiers = _identifiers(window)

    segmented = build_evidence_set(window, segments=segments)
    inline_only = build_evidence_set(window, segments=())

    expected = hashlib.sha256(
        json.dumps(
            [str(identifier) for identifier in identifiers],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert segmented.canonical_hash == expected == inline_only.canonical_hash
    assert segmented.evidence_set_id == inline_only.evidence_set_id
    assert resolve_evidence_set(inline_only, ()) == identifiers
    assert segmented.segment_ids and not inline_only.segment_ids


def test_identities_are_deterministic_and_exclude_metric_key_cut_value_and_window() -> None:
    """A4: identities are deterministic and their preimages exclude execution coordinates."""
    series = _series(48)
    first_segments = build_evidence_segments(series)
    second_segments = build_evidence_segments(series)
    window = series[24:48]

    assert [segment.segment_id for segment in first_segments] == [
        segment.segment_id for segment in second_segments
    ]
    evidence = build_evidence_set(window, segments=first_segments)
    repeated = build_evidence_set(window, segments=second_segments)
    assert evidence.evidence_set_id == repeated.evidence_set_id
    assert evidence.evidence_set_id.version == 8

    shifted = build_evidence_set(series[23:47], segments=first_segments)
    assert shifted.evidence_set_id != evidence.evidence_set_id
    assert shifted.canonical_hash != evidence.canonical_hash
    identifiers = _identifiers(window)
    assert canonical_lineage_hash(identifiers) != canonical_lineage_hash(
        tuple(reversed(identifiers))
    )

    revised_series = tuple(
        _observation(index, revision="revised") if index == 29 else observation
        for index, observation in enumerate(series)
    )
    revised_segments = build_evidence_segments(revised_series)
    revised_evidence = build_evidence_set(revised_series[24:48], segments=revised_segments)
    assert [segment.segment_id for segment in revised_segments] != [
        segment.segment_id for segment in first_segments
    ]
    assert revised_evidence.evidence_set_id != evidence.evidence_set_id

    for overrides in (
        {"asset_id": "crypto:eth-usd"},
        {"source_id": "deribit:other:funding-rate-history"},
        {"field_name": "funding_interest_8h"},
    ):
        other = _series(48, **overrides)
        other_evidence = build_evidence_set(other[24:48], segments=build_evidence_segments(other))
        assert other_evidence.evidence_set_id != evidence.evidence_set_id

    assert "metric_key" not in EvidenceSet.model_fields
    assert "known_at" not in EvidenceSet.model_fields
    assert "computed_at" not in EvidenceSet.model_fields
    assert "value" not in EvidenceSet.model_fields
    assert "window" not in EvidenceSet.model_fields
    assert set(EvidenceSet.model_fields) == {
        "evidence_set_id",
        "segmentation_policy",
        "asset_id",
        "source_id",
        "field_name",
        "input_count",
        "segment_ids",
        "head_offset",
        "inline_observation_ids",
        "inline_available_at",
        "first_observed_at",
        "first_observation_id",
        "last_observed_at",
        "last_observation_id",
        "available_at",
        "canonical_hash",
    }


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-segment", "missing or unresolvable"),
        ("missing-inputs", "window exceeds the referenced segments"),
        ("extra-inline-input", "inline tail does not fit the window"),
        ("repeated-segment", "not consecutive"),
        ("reordered-segments", "not consecutive"),
        ("shifted-segment-day", "not consecutive"),
        ("revised-input", "hash does not match"),
        ("other-scope", "mixes asset, source or field"),
        ("altered-segment", "hash does not match"),
        ("altered-identity", "identity does not match its content"),
        ("inconsistent-availability", "available_at is not the maximum"),
    ],
)
def test_verification_fails_closed_on_every_corruption_case(case: str, message: str) -> None:
    """A5: every corruption fails closed with a typed error instead of a valid set."""
    series = _series(77)
    segments = build_evidence_segments(series)
    window = series[30:77]  # partial day two, complete day three and a five-hour tail
    evidence = build_evidence_set(window, segments=segments)
    assert len(evidence.segment_ids) == 2
    first, second = segments[1], segments[2]
    supplied: tuple[EvidenceSegment, ...] = (first, second)
    corrupted = evidence

    if case == "missing-segment":
        supplied = (first,)
    elif case == "missing-inputs":
        corrupted = evidence.model_copy(
            update={"inline_observation_ids": (), "inline_available_at": None}
        )
    elif case == "extra-inline-input":
        corrupted = evidence.model_copy(
            update={
                "inline_observation_ids": evidence.inline_observation_ids + (uuid4(),),
            }
        )
    elif case == "repeated-segment":
        corrupted = evidence.model_copy(
            update={"segment_ids": evidence.segment_ids + (first.segment_id,)}
        )
    elif case == "reordered-segments":
        corrupted = evidence.model_copy(
            update={"segment_ids": tuple(reversed(evidence.segment_ids))}
        )
    elif case == "shifted-segment-day":
        elsewhere = build_evidence_segments(tuple(_observation(96 + index) for index in range(24)))[
            0
        ]
        corrupted = evidence.model_copy(
            update={"segment_ids": (first.segment_id, elsewhere.segment_id)}
        )
        supplied = (first, elsewhere)
    elif case == "revised-input":
        changed = first.model_copy(
            update={"observation_ids": (uuid4(),) + first.observation_ids[1:]}
        )
        supplied = (changed, second)
    elif case == "other-scope":
        changed = first.model_copy(update={"asset_id": "crypto:eth-usd"})
        supplied = (changed, second)
    elif case == "altered-segment":
        changed = first.model_copy(
            update={"observation_ids": tuple(reversed(first.observation_ids))}
        )
        supplied = (changed, second)
    elif case == "altered-identity":
        forged = uuid4()
        changed = first.model_copy(update={"segment_id": forged})
        corrupted = evidence.model_copy(update={"segment_ids": (forged, second.segment_id)})
        supplied = (changed, second)
    elif case == "inconsistent-availability":
        corrupted = evidence.model_copy(
            update={"available_at": evidence.available_at + timedelta(hours=1)}
        )

    with pytest.raises(EvidenceSetVerificationError, match=message):
        verify_evidence_set(corrupted, supplied)


def test_segments_exist_only_for_complete_utc_days() -> None:
    """N1: immutable segments exist only for days with their 24 hours present."""
    assert build_evidence_segments(_series(23)) == ()
    assert len(build_evidence_segments(_series(47))) == 1
    assert len(build_evidence_segments(_series(48))) == 2

    series = _series(53)
    segments = build_evidence_segments(series)
    assert [segment.day for segment in segments] == [date(2026, 6, 1), date(2026, 6, 2)]
    assert all(len(segment.observation_ids) == 24 for segment in segments)

    incomplete = build_evidence_set(series[48:53], segments=segments)
    assert incomplete.segment_ids == ()
    assert incomplete.inline_observation_ids == _identifiers(series[48:53])

    with pytest.raises(EvidenceSetConstructionError, match="final incomplete day"):
        build_evidence_set(_series(30), segments=())


def test_evidence_identifiers_are_never_metric_identifiers() -> None:
    """N2: segment and set identities are their own domain, not metric identities."""
    series = _series(48)
    segments = build_evidence_segments(series)
    window = series[24:48]
    evidence = build_evidence_set(window, segments=segments)
    window_ids = _identifiers(window)

    metric_identity = metric_result_id_v2(
        asset_id=_ASSET,
        metric_key="crypto.derivatives.funding.sum_1h",
        input_observation_ids=window_ids,
        algorithm_version="crypto-derivatives-metrics-v1-decimal34",
        as_of=window[-1].observed_at,
        available_at=_AVAILABLE,
        unit="ratio",
        quality=DataQuality.VALID,
        parameters={
            "formula": "sum(funding_interest_1h)",
            "source_ids": [_SOURCE],
            "window": 24,
        },
    )
    evidence_identifiers = {segment.segment_id for segment in segments} | {evidence.evidence_set_id}

    assert metric_identity.version == 8
    assert all(identifier.version == 8 for identifier in evidence_identifiers)
    assert metric_identity not in evidence_identifiers
    assert not evidence_identifiers & set(window_ids)
    assert "input_metric_result_ids" not in EvidenceSet.model_fields
    assert "metric_key" not in EvidenceSegment.model_fields


def test_module_is_pure_and_has_no_production_importer() -> None:
    """A9: the module is pure and no production module imports it."""
    source = Path(evidence_set_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "investment_analyst.storage",
        "investment_analyst.workspace",
        "investment_analyst.providers",
        "investment_analyst.analytics.metric_identity",
        "derivatives_engine",
        "derivatives_pipeline",
        "derivatives_service",
    ):
        assert f"from {forbidden}" not in source
        assert f"import {forbidden}" not in source
    for forbidden_attribute in (
        "open(",
        "write_text",
        "write_bytes",
        "pathlib",
        "os.",
        "datetime.now",
        "time.time",
    ):
        assert forbidden_attribute not in source
    imported_modules = {
        value.__name__
        for value in vars(evidence_set_module).values()
        if isinstance(value, ModuleType)
    }
    assert imported_modules == {"hashlib", "json"}

    root = Path(evidence_set_module.__file__).parents[1]
    importers = [
        path
        for path in sorted(root.rglob("*.py"))
        if path.name != "evidence_set.py" and "evidence_set" in path.read_text(encoding="utf-8")
    ]
    assert importers == []
