"""Which summaries are late, without a database (ADR 0023). Builds are in test_totals_db.py."""

from datetime import UTC, datetime, timedelta

from app.db.totals import FIVE_MINUTES, late_buckets

NOON = datetime(2026, 10, 18, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def test_late_means_its_bucket_may_be_built() -> None:
    # The 11:55 bucket ended at 12:00: late from 12:02, built from 12:03.
    assert late_buckets([NOON - MINUTE], NOON + 2 * MINUTE - timedelta(seconds=1)) == []
    assert late_buckets([NOON - MINUTE, NOON - 4 * MINUTE], NOON + 2 * MINUTE) == [
        NOON - FIVE_MINUTES
    ]
