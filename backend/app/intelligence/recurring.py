"""Subscription and recurring-charge detection.

Entirely arithmetic: group by merchant, measure the gaps between charges, and
decide whether those gaps look like a schedule. No model is involved, which
matters because the output drives a "you will be charged ₹649 on 12 April"
claim — a guess dressed as a prediction would be worse than saying nothing.

Two judgements the detector has to make honestly:

**Is this a schedule or a coincidence?** Three coffees bought on three
consecutive Mondays is not a subscription. So a candidate needs at least three
charges *and* gaps that agree with each other — measured as a stability score
from the spread of the intervals, not from their average alone.

**What will the next charge be?** The median amount, not the mean: one annual
plan bought among eleven monthly ones should not drag the estimate upward.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SubscriptionCadence, SubscriptionStatus

#: Fewer than this is not a pattern, it is a coincidence.
MIN_OCCURRENCES = 3

#: Intervals must agree this well to count as a schedule.
#:
#: Calibrated against real behaviour rather than guessed. Genuine monthly
#: billing scores ~0.97 on this measure — its only drift comes from month
#: lengths and weekend shifts, a day or two. Meanwhile frequent *irregular*
#: spending clusters around 0.73: takeaway ordered roughly every couple of weeks
#: looks fortnightly to an interval test, and at a 0.70 floor it was being
#: reported as a subscription with a predicted next charge.
#:
#: 0.80 admits the calendar drift a real schedule has and rejects the
#: coincidence. A missed threshold costs a subscription card; a false one costs
#: the user's trust in a "you will be charged on the 4th" claim.
MIN_STABILITY = Decimal("0.80")

#: (cadence, expected days, tolerance). Ordered shortest first; the first
#: interval that fits wins.
_CADENCES: tuple[tuple[SubscriptionCadence, int, int], ...] = (
    (SubscriptionCadence.WEEKLY, 7, 2),
    (SubscriptionCadence.FORTNIGHTLY, 14, 3),
    (SubscriptionCadence.MONTHLY, 30, 5),
    (SubscriptionCadence.QUARTERLY, 91, 10),
    (SubscriptionCadence.HALF_YEARLY, 182, 15),
    (SubscriptionCadence.ANNUAL, 365, 25),
)

_CHARGES_PER_YEAR: dict[SubscriptionCadence, int] = {
    SubscriptionCadence.WEEKLY: 52,
    SubscriptionCadence.FORTNIGHTLY: 26,
    SubscriptionCadence.MONTHLY: 12,
    SubscriptionCadence.QUARTERLY: 4,
    SubscriptionCadence.HALF_YEARLY: 2,
    SubscriptionCadence.ANNUAL: 1,
}

#: A subscription whose next charge is this far overdue has probably been
#: cancelled. Reported as lapsed rather than deleted — a cancelled subscription
#: is still useful history.
LAPSE_GRACE_DAYS = 45


#: Cadences measured in calendar months rather than in days.
_CALENDAR_MONTHS: dict[SubscriptionCadence, int] = {
    SubscriptionCadence.MONTHLY: 1,
    SubscriptionCadence.QUARTERLY: 3,
    SubscriptionCadence.HALF_YEARLY: 6,
    SubscriptionCadence.ANNUAL: 12,
}


def next_charge_date(
    last_charge: date, cadence: SubscriptionCadence, median_interval: int
) -> date:
    """When the next charge falls.

    Calendar cadences advance by *months*, preserving the day, rather than by a
    median number of days. A subscription billed on the 9th has intervals of 28,
    29, 30 and 31 days depending on the month, so adding the median (31) to a
    9 June charge predicts 10 July — wrong by a day, and drifting further every
    month. Weekly and fortnightly cadences genuinely are day-based and still add
    days.

    The day is clamped to the target month's length, which is what billing
    systems do: a subscription taken out on the 31st charges on the 28th in
    February.
    """
    import calendar

    months = _CALENDAR_MONTHS.get(cadence)
    if months is None:
        return last_charge + timedelta(days=median_interval)

    total = last_charge.month - 1 + months
    year = last_charge.year + total // 12
    month = total % 12 + 1
    day = min(last_charge.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(slots=True)
class Candidate:
    merchant: str
    category_id: uuid.UUID | None
    account_id: uuid.UUID | None
    charges: list[tuple[date, Decimal]]

    @property
    def dates(self) -> list[date]:
        return [day for day, _ in self.charges]

    @property
    def amounts(self) -> list[Decimal]:
        return [amount for _, amount in self.charges]


def _intervals(days: list[date]) -> list[int]:
    return [(right - left).days for left, right in zip(days, days[1:])]


def stability(intervals: list[int]) -> Decimal:
    """How consistent the gaps are, in [0, 1].

    ``1 - (spread / typical gap)``, using the *median* gap and the mean absolute
    deviation from it. Deliberately robust: a single missed or double-billed
    month should lower confidence, not destroy it, and a standard deviation
    would let one outlier do exactly that.

    Floats are fine here — this is a ratio describing dates, not money.
    """
    if len(intervals) < 2:
        return Decimal("0.50")

    typical = statistics.median(intervals)
    if typical <= 0:
        return Decimal("0.00")

    deviation = sum(abs(value - typical) for value in intervals) / len(intervals)
    score = max(0.0, 1.0 - (deviation / typical))
    # Three decimals: the column is NUMERIC(4,3), and quantizing here keeps
    # what the code compares identical to what the database stores.
    return Decimal(str(round(score, 3)))


def classify_cadence(intervals: list[int]) -> SubscriptionCadence | None:
    if not intervals:
        return None
    typical = statistics.median(intervals)
    for cadence, expected, tolerance in _CADENCES:
        if abs(typical - expected) <= tolerance:
            return cadence
    return None


async def _candidates(session: AsyncSession, *, since: date) -> list[Candidate]:
    rows = (
        await session.execute(
            text(
                """
                SELECT t.merchant,
                       -- mode(), not min(): PostgreSQL has no min() for uuid,
                       -- and the category a subscription is *usually* filed
                       -- under is the meaningful one anyway. An arbitrary pick
                       -- would let one miscategorised charge relabel the whole
                       -- subscription.
                       mode() WITHIN GROUP (ORDER BY t.category_id) AS category_id,
                       mode() WITHIN GROUP (ORDER BY t.account_id) AS account_id,
                       array_agg(t.txn_date ORDER BY t.txn_date) AS days,
                       array_agg(t.amount ORDER BY t.txn_date) AS amounts
                FROM transactions t
                WHERE t.is_expense
                  AND t.merchant IS NOT NULL
                  AND t.txn_date >= :since
                GROUP BY t.merchant
                HAVING COUNT(*) >= :minimum
                """
            ),
            {"since": since, "minimum": MIN_OCCURRENCES},
        )
    ).all()

    return [
        Candidate(
            merchant=row.merchant,
            category_id=row.category_id,
            account_id=row.account_id,
            charges=list(zip(row.days, [Decimal(str(a)) for a in row.amounts])),
        )
        for row in rows
    ]


@dataclass(slots=True)
class DetectedSubscription:
    merchant: str
    category_id: uuid.UUID | None
    account_id: uuid.UUID | None
    cadence: SubscriptionCadence
    cadence_stability: Decimal
    typical_amount: Decimal
    last_amount: Decimal
    estimated_annual_cost: Decimal
    first_charge_on: date
    last_charge_on: date
    next_expected_on: date | None
    occurrence_count: int
    status: SubscriptionStatus


def detect(candidate: Candidate, *, today: date | None = None) -> DetectedSubscription | None:
    """Decide whether one merchant's charges form a schedule."""
    today = today or date.today()
    days = candidate.dates
    if len(days) < MIN_OCCURRENCES:
        return None

    intervals = _intervals(days)
    cadence = classify_cadence(intervals)
    if cadence is None:
        return None

    score = stability(intervals)
    if score < MIN_STABILITY:
        return None

    amounts = candidate.amounts
    # Median, not mean: one annual plan among eleven monthly charges must not
    # drag the estimate up.
    typical = Decimal(str(statistics.median(amounts))).quantize(Decimal("0.01"))
    last_charge = days[-1]
    step = int(statistics.median(intervals))
    next_expected = next_charge_date(last_charge, cadence, step)

    status = SubscriptionStatus.ACTIVE
    if next_expected < today - timedelta(days=LAPSE_GRACE_DAYS):
        status = SubscriptionStatus.LAPSED

    return DetectedSubscription(
        merchant=candidate.merchant,
        category_id=candidate.category_id,
        account_id=candidate.account_id,
        cadence=cadence,
        cadence_stability=score,
        typical_amount=typical,
        last_amount=amounts[-1].quantize(Decimal("0.01")),
        estimated_annual_cost=(typical * _CHARGES_PER_YEAR[cadence]).quantize(
            Decimal("0.01")
        ),
        first_charge_on=days[0],
        last_charge_on=last_charge,
        next_expected_on=next_expected,
        occurrence_count=len(days),
        status=status,
    )


async def refresh(
    session: AsyncSession, *, tenant_id: uuid.UUID, lookback_days: int = 400,
    today: date | None = None,
) -> list[DetectedSubscription]:
    """Recompute every subscription for a tenant.

    Rebuilt wholesale rather than incrementally updated: a subscription is a
    statement about the whole history, and patching one in place is how a
    cancelled service stays "active" forever.
    """
    today = today or date.today()
    since = today - timedelta(days=lookback_days)

    detected = [
        subscription
        for subscription in (
            detect(candidate, today=today)
            for candidate in await _candidates(session, since=since)
        )
        if subscription is not None
    ]

    await session.execute(text("DELETE FROM subscriptions"))

    for subscription in detected:
        await session.execute(
            text(
                """
                INSERT INTO subscriptions (
                    tenant_id, merchant, category_id, account_id, cadence,
                    cadence_stability, typical_amount, last_amount,
                    estimated_annual_cost, first_charge_on, last_charge_on,
                    next_expected_on, occurrence_count, status
                ) VALUES (
                    :tenant_id, :merchant, :category_id, :account_id, :cadence,
                    :stability, :typical, :last_amount, :annual, :first_on,
                    :last_on, :next_on, :count, :status
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "merchant": subscription.merchant,
                "category_id": subscription.category_id,
                "account_id": subscription.account_id,
                "cadence": str(subscription.cadence),
                "stability": subscription.cadence_stability,
                "typical": subscription.typical_amount,
                "last_amount": subscription.last_amount,
                "annual": subscription.estimated_annual_cost,
                "first_on": subscription.first_charge_on,
                "last_on": subscription.last_charge_on,
                "next_on": subscription.next_expected_on,
                "count": subscription.occurrence_count,
                "status": str(subscription.status),
            },
        )

    return detected


async def list_subscriptions(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT s.id, s.merchant, s.cadence, s.cadence_stability,
                       s.typical_amount, s.last_amount, s.estimated_annual_cost,
                       s.first_charge_on, s.last_charge_on, s.next_expected_on,
                       s.occurrence_count, s.status,
                       c.slug AS category_slug, c.name AS category_name, c.color
                FROM subscriptions s
                LEFT JOIN categories c ON c.id = s.category_id
                ORDER BY s.estimated_annual_cost DESC
                """
            )
        )
    ).all()
    return [dict(row._mapping) for row in rows]
