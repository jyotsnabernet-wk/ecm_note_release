"""
Planned Wednesday release cadence — reporting window used when gathering on Monday.

Window: **last Thursday 00:00 UTC** through **end of the upcoming Wednesday UTC**
(the Wednesday on or after the anchor date), inclusive of that Wednesday.

The start date is **six calendar days before** that Wednesday (the **Thursday** opening
the Thu→Wed release block that ends on deploy Wednesday).

Internally represented as half-open ``[start_utc, end_exclusive_utc)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


def next_wednesday_on_or_after(d: date) -> date:
    """Calendar Wednesday on or after ``d`` (ISO weekday Mon=1 … Wed=3)."""
    w = d.isoweekday()
    delta = (3 - w) % 7
    return d + timedelta(days=delta)


@dataclass(frozen=True)
class PlannedReleaseWindow:
    anchor_date_utc: date
    start_utc: datetime
    end_exclusive_utc: datetime
    release_wednesday: date

    @property
    def start_date(self) -> date:
        return self.start_utc.date()

    @property
    def end_inclusive_date(self) -> date:
        return self.end_exclusive_utc.date() - timedelta(days=1)


def planned_window_for_anchor(anchor: datetime | None = None) -> PlannedReleaseWindow:
    """
    Compute the Thu→Wed window whose **deploy day** is the Wednesday on or after ``anchor``.

    ``start`` = deploy Wednesday minus **6** calendar days (always a **Thursday** 00:00 UTC).
    ``end_exclusive`` = Thursday 00:00 UTC immediately after the deploy Wednesday.
    """
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    elif anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    else:
        anchor = anchor.astimezone(timezone.utc)

    d = anchor.date()
    release_wed = next_wednesday_on_or_after(d)
    start_date = release_wed - timedelta(days=6)
    start_utc = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_exclusive_utc = datetime.combine(release_wed + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return PlannedReleaseWindow(
        anchor_date_utc=d,
        start_utc=start_utc,
        end_exclusive_utc=end_exclusive_utc,
        release_wednesday=release_wed,
    )
