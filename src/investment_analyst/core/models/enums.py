"""Enumerations shared by the core data contracts."""

from enum import StrEnum


class AssetClass(StrEnum):
    """Supported asset classes for the initial project scope."""

    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"


class SourceType(StrEnum):
    """Broad type of information supplied by a source."""

    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    MACRO = "macro"
    ONCHAIN = "onchain"
    NEWS = "news"


class DataFrequency(StrEnum):
    """Frequency or reporting cadence of a data point."""

    TICK = "tick"
    MINUTE_1 = "minute_1"
    MINUTE_5 = "minute_5"
    MINUTE_15 = "minute_15"
    HOUR_1 = "hour_1"
    DAY_1 = "day_1"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    EVENT = "event"


class DataQuality(StrEnum):
    """Quality status attached to normalized and calculated data."""

    VALID = "valid"
    DELAYED = "delayed"
    PARTIAL = "partial"
    SUSPECT = "suspect"


class MetricCategory(StrEnum):
    """Analysis area to which a metric belongs."""

    MARKET = "market"
    FUNDAMENTAL = "fundamental"
    CRYPTO_FUNDAMENTAL = "crypto_fundamental"
    UNIFIED = "unified"
    CAZATIBURONES = "cazatiburones"
    DATA_QUALITY = "data_quality"


class DiagnosticMode(StrEnum):
    """Independent diagnostic modes supported by the architecture."""

    MARKET = "market"
    FUNDAMENTAL = "fundamental"
    UNIFIED = "unified"


class DiagnosticVerdict(StrEnum):
    """High-level outcome of a deterministic diagnostic."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    INSUFFICIENT_DATA = "insufficient_data"


class EvidenceDirection(StrEnum):
    """Direction in which evidence affects a diagnostic."""

    SUPPORTS = "supports"
    OPPOSES = "opposes"
    NEUTRAL = "neutral"
