"""Neutral UTC bounds for user-facing inclusive calendar-date intervals."""

from datetime import UTC, date, datetime, time, timedelta


def inclusive_utc_date_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Return a half-open UTC interval that includes both supplied calendar dates."""
    if isinstance(start, datetime) or isinstance(end, datetime):
        raise TypeError("inclusive date bounds require date values without times")
    if start > end:
        raise ValueError("start date must not be later than end date")
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )
