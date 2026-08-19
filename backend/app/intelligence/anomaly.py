"""Statistical outlier detection.

These are **outliers with stated reasons, never fraud claims.** The system has
no ground truth for fraud, and every signal here has innocent explanations — a
large first purchase at a new shop is usually a large first purchase at a new
shop. So the vocabulary is "unusual", the reason string carries the actual
numbers that triggered it, and the user decides what it means.

Robust statistics throughout: median and median absolute deviation rather than
mean and standard deviation. Spending distributions are skewed and small — one
rent payment in a month of groceries would drag a mean far enough that nothing
else could ever look unusual, and would then make the rent itself look normal.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AnomalyKind

#: Below this many prior observations there is no baseline worth comparing to.
MIN_HISTORY = 5

#: Robust z-score threshold. 3.5 on the MAD scale is the conventional cut for
#: "unusual", and on real spending it fires rarely enough to stay meaningful.
Z_THRESHOLD = Decimal("3.5")

#: MAD is scaled by this so its z-scores are comparable to standard deviations
#: for normally distributed data.
_MAD_SCALE = Decimal("1.4826")

#: A category has to move by more than this, and by a material amount, before a
#: month-on-month change is worth mentioning.
SPIKE_RATIO = Decimal("2.0")
SPIKE_MINIMUM = Decimal("2000.00")

#: Two identical charges at one merchant this close together are worth a look.
DUPLICATE_WINDOW_DAYS = 3

#: A first-ever charge at a merchant this much above the user's typical
#: transaction is worth surfacing.
FIRST_SEEN_MULTIPLE = Decimal("3.0")


@dataclass(slots=True)
class DetectedAnomaly:
    kind: AnomalyKind
    detected_on: date
    reason: str
    transaction_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    merchant: str | None = None
    period_month: date | None = None
    observed_value: Decimal | None = None
    baseline_value: Decimal | None = None
    deviation_score: Decimal | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _inr(value: Decimal) -> str:
    """Indian digit grouping for a reason string a person will read."""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"₹{whole}.{frac}"


def robust_z(value: Decimal, history: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    """(z-score, median) using median absolute deviation.

    Returns None when the history is too short or has no spread at all — a MAD
    of zero means every prior charge was identical, and dividing by it would
    report an infinite deviation for a one-rupee difference.
    """
    if len(history) < MIN_HISTORY:
        return None

    median = Decimal(str(statistics.median(history)))
    deviations = [abs(item - median) for item in history]
    mad = Decimal(str(statistics.median(deviations)))
    if mad <= 0:
        return None

    z = (abs(value - median) / (mad * _MAD_SCALE)).quantize(Decimal("0.01"))
    return z, median.quantize(Decimal("0.01"))


async def amount_outliers(
    session: AsyncSession, *, month: date, today: date | None = None
) -> list[DetectedAnomaly]:
    """Transactions far from what this category normally costs."""
    from app.intelligence.analytics import month_bounds

    first, last = month_bounds(month)
    rows = (
        await session.execute(
            text(
                """
                SELECT t.id, t.amount, t.txn_date, t.merchant, t.category_id,
                       c.name AS category_name
                FROM transactions t
                JOIN categories c ON c.id = t.category_id
                WHERE t.is_expense AND t.txn_date BETWEEN :first AND :last
                ORDER BY t.amount DESC
                """
            ),
            {"first": first, "last": last},
        )
    ).all()

    found: list[DetectedAnomaly] = []
    history_cache: dict[uuid.UUID, list[Decimal]] = {}

    for row in rows:
        if row.category_id not in history_cache:
            prior = (
                await session.execute(
                    text(
                        """
                        SELECT t.amount FROM transactions t
                        WHERE t.is_expense
                          AND t.category_id = :category_id
                          AND t.txn_date < :first
                        ORDER BY t.txn_date DESC
                        LIMIT 200
                        """
                    ),
                    {"category_id": row.category_id, "first": first},
                )
            ).scalars().all()
            history_cache[row.category_id] = [Decimal(str(value)) for value in prior]

        scored = robust_z(_money(row.amount), history_cache[row.category_id])
        if scored is None:
            continue
        z, median = scored
        if z < Z_THRESHOLD:
            continue

        found.append(
            DetectedAnomaly(
                kind=AnomalyKind.AMOUNT_OUTLIER,
                detected_on=row.txn_date,
                transaction_id=row.id,
                category_id=row.category_id,
                merchant=row.merchant,
                period_month=first,
                observed_value=_money(row.amount),
                baseline_value=median,
                # Normalised for storage; the raw score is in the evidence.
                deviation_score=min(z / Decimal("10"), Decimal("1")).quantize(
                    Decimal("0.001")
                ),
                # `robust_z` measures distance from the median in either
                # direction — `abs(value - median)` — so the sentence has to
                # say which direction. Calling a ₹536 charge "unusually large"
                # against a ₹1,214 median is not a rounding error in the
                # wording, it is the explanation contradicting the figures
                # printed beside it.
                reason=(
                    f"{_inr(_money(row.amount))} at "
                    f"{row.merchant or 'an unnamed payee'} is unusually "
                    f"{'large' if _money(row.amount) > median else 'small'} for "
                    f"{row.category_name}, where you typically spend "
                    f"{_inr(median)}."
                ),
                evidence={"robust_z": str(z), "median": str(median)},
            )
        )

    return found


async def category_spikes(
    session: AsyncSession, *, month: date, baseline_months: int = 3
) -> list[DetectedAnomaly]:
    """A category that jumped against its own recent trailing average."""
    from app.intelligence.analytics import month_bounds, previous_month

    first, last = month_bounds(month)
    baseline_start = first
    for _ in range(baseline_months):
        baseline_start = previous_month(baseline_start)

    rows = (
        await session.execute(
            text(
                """
                WITH current AS (
                    SELECT t.category_id, SUM(t.amount) AS total
                    FROM transactions t
                    WHERE t.is_expense AND t.txn_date BETWEEN :first AND :last
                    GROUP BY t.category_id
                ),
                baseline AS (
                    SELECT t.category_id,
                           SUM(t.amount) / GREATEST(
                               COUNT(DISTINCT date_trunc('month', t.txn_date)), 1
                           ) AS monthly_average,
                           COUNT(DISTINCT date_trunc('month', t.txn_date)) AS months
                    FROM transactions t
                    WHERE t.is_expense
                      AND t.txn_date >= :baseline_start
                      AND t.txn_date < :first
                    GROUP BY t.category_id
                )
                SELECT c.id AS category_id, c.name AS category_name,
                       current.total, baseline.monthly_average, baseline.months
                FROM current
                JOIN baseline ON baseline.category_id = current.category_id
                JOIN categories c ON c.id = current.category_id
                WHERE baseline.months >= 2
                """
            ),
            {"first": first, "last": last, "baseline_start": baseline_start},
        )
    ).all()

    found: list[DetectedAnomaly] = []
    for row in rows:
        total = _money(row.total)
        average = _money(row.monthly_average)
        if average <= 0:
            continue
        increase = total - average
        if total < average * SPIKE_RATIO or increase < SPIKE_MINIMUM:
            # Both tests, deliberately: a 3× jump on ₹200 is noise, and a ₹3,000
            # rise on ₹40,000 of rent is not a spike.
            continue

        found.append(
            DetectedAnomaly(
                kind=AnomalyKind.CATEGORY_SPIKE,
                detected_on=last,
                category_id=row.category_id,
                period_month=first,
                observed_value=total,
                baseline_value=average,
                deviation_score=min(
                    (total / average / Decimal("10")), Decimal("1")
                ).quantize(Decimal("0.001")),
                reason=(
                    f"{row.category_name} came to {_inr(total)} this month, against "
                    f"an average of {_inr(average)} over the previous "
                    f"{int(row.months)} months — {_inr(increase)} more than usual."
                ),
                evidence={
                    "ratio": str((total / average).quantize(Decimal("0.01"))),
                    "baseline_months": int(row.months),
                },
            )
        )
    return found


async def duplicate_charges(
    session: AsyncSession, *, month: date
) -> list[DetectedAnomaly]:
    """Two identical charges at one merchant within a few days.

    Distinct from the ledger's duplicate detection, which asks "is this the same
    transaction imported twice?". This asks "were you billed twice?" — a real
    event with real money, not an import artefact.
    """
    from app.intelligence.analytics import month_bounds

    first, last = month_bounds(month)
    rows = (
        await session.execute(
            text(
                """
                SELECT a.id AS left_id, b.id AS right_id, a.merchant, a.amount,
                       a.txn_date AS left_date, b.txn_date AS right_date,
                       a.category_id
                FROM transactions a
                JOIN transactions b
                  ON b.merchant = a.merchant
                 AND b.amount = a.amount
                 AND b.direction = a.direction
                 AND b.id <> a.id
                 AND b.txn_date > a.txn_date
                 AND b.txn_date <= a.txn_date + make_interval(days => :window)
                WHERE a.is_expense
                  AND a.merchant IS NOT NULL
                  AND a.txn_date BETWEEN :first AND :last
                  -- Not two sides of one import: those share nothing here.
                  AND a.statement_id IS DISTINCT FROM NULL
                ORDER BY a.txn_date
                """
            ),
            {"first": first, "last": last, "window": DUPLICATE_WINDOW_DAYS},
        )
    ).all()

    return [
        DetectedAnomaly(
            kind=AnomalyKind.DUPLICATE_PROXIMITY,
            detected_on=row.right_date,
            transaction_id=row.right_id,
            category_id=row.category_id,
            merchant=row.merchant,
            period_month=first,
            observed_value=_money(row.amount),
            reason=(
                f"{row.merchant} charged {_inr(_money(row.amount))} twice within "
                f"{(row.right_date - row.left_date).days} day(s). This may be a "
                "genuine repeat purchase, or a double charge worth checking."
            ),
            evidence={
                "first_on": row.left_date.isoformat(),
                "second_on": row.right_date.isoformat(),
            },
        )
        for row in rows
    ]


async def first_seen_large(
    session: AsyncSession, *, month: date
) -> list[DetectedAnomaly]:
    """A first-ever charge at a merchant, far above your usual transaction."""
    from app.intelligence.analytics import month_bounds

    first, last = month_bounds(month)

    typical = (
        await session.execute(
            text(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median
                FROM transactions
                WHERE is_expense AND txn_date < :last
                """
            ),
            {"last": last},
        )
    ).scalar_one_or_none()
    if typical is None:
        return []
    median = _money(typical)
    if median <= 0:
        return []

    rows = (
        await session.execute(
            text(
                """
                SELECT t.id, t.merchant, t.amount, t.txn_date, t.category_id
                FROM transactions t
                WHERE t.is_expense
                  AND t.merchant IS NOT NULL
                  AND t.txn_date BETWEEN :first AND :last
                  AND t.amount >= :floor
                  AND NOT EXISTS (
                      SELECT 1 FROM transactions prior
                      WHERE prior.merchant = t.merchant
                        AND prior.txn_date < :first
                  )
                """
            ),
            {"first": first, "last": last, "floor": median * FIRST_SEEN_MULTIPLE},
        )
    ).all()

    return [
        DetectedAnomaly(
            kind=AnomalyKind.MERCHANT_FIRST_LARGE,
            detected_on=row.txn_date,
            transaction_id=row.id,
            category_id=row.category_id,
            merchant=row.merchant,
            period_month=first,
            observed_value=_money(row.amount),
            baseline_value=median,
            reason=(
                f"First payment to {row.merchant}, for "
                f"{_inr(_money(row.amount))} — well above your typical "
                f"transaction of {_inr(median)}."
            ),
            evidence={"typical_transaction": str(median)},
        )
        for row in rows
    ]


async def sweep(
    session: AsyncSession, *, tenant_id: uuid.UUID, month: date
) -> list[DetectedAnomaly]:
    """Run every detector for a month and persist the results."""
    from app.intelligence.analytics import month_bounds

    first, _ = month_bounds(month)

    found: list[DetectedAnomaly] = []
    found += await amount_outliers(session, month=month)
    found += await category_spikes(session, month=month)
    found += await duplicate_charges(session, month=month)
    found += await first_seen_large(session, month=month)

    # Recomputed wholesale for the month: an anomaly that no longer holds after
    # a correction should disappear, not linger as a stale warning.
    await session.execute(
        text("DELETE FROM anomalies WHERE period_month = :month"), {"month": first}
    )

    import json

    for anomaly in found:
        await session.execute(
            text(
                """
                -- No merchant column: the merchant lives on the transaction
                -- this anomaly points at, and a category spike has no single
                -- merchant at all. Duplicating it here would create a second
                -- copy that drifts when the transaction is corrected.
                INSERT INTO anomalies (
                    tenant_id, kind, transaction_id, category_id,
                    detected_on, period_month, observed_value, baseline_value,
                    deviation_score, reason, evidence
                ) VALUES (
                    :tenant_id, :kind, :transaction_id, :category_id,
                    :detected_on, :period_month, :observed, :baseline,
                    :score, :reason, CAST(:evidence AS jsonb)
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "kind": str(anomaly.kind),
                "transaction_id": anomaly.transaction_id,
                "category_id": anomaly.category_id,
                "detected_on": anomaly.detected_on,
                "period_month": anomaly.period_month,
                "observed": anomaly.observed_value,
                "baseline": anomaly.baseline_value,
                "score": anomaly.deviation_score,
                "reason": anomaly.reason,
                "evidence": json.dumps(anomaly.evidence),
            },
        )

    return found


async def list_anomalies(
    session: AsyncSession, *, limit: int = 50
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT a.id, a.kind, t.merchant, a.detected_on, a.period_month,
                       a.observed_value, a.baseline_value, a.deviation_score,
                       a.reason, a.evidence, a.transaction_id,
                       c.slug AS category_slug, c.name AS category_name
                FROM anomalies a
                LEFT JOIN categories c ON c.id = a.category_id
                LEFT JOIN transactions t ON t.id = a.transaction_id
                ORDER BY a.detected_on DESC, a.observed_value DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).all()
    return [dict(row._mapping) for row in rows]
