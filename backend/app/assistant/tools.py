"""The seven things the assistant can do, and nothing else.

Every tool is a thin wrapper over the Financial Intelligence Engine. None of
them computes a figure of its own: the numbers here are the same numbers the
dashboard renders, produced by the same functions, which is why an answer and
the chart beside it can never disagree.

Three properties are structural rather than conventional:

**Identity is not expressible.** Each tool's arguments are a Pydantic model
with ``extra="forbid"`` and no tenant, user or account-id field. The session is
tenant-scoped by the FastAPI dependency, from the access token. A model that
emits ``{"tenant_id": "..."}`` does not get someone else's data and does not
get a warning either — construction fails, because there is nowhere to put it.
There is no argument by which the assistant can be pointed at another tenant.

**The model gets a projection, not the row.** Every tool builds two views. The
``display`` view is exact and complete and goes to the browser, where the data
belongs to the person reading it. The ``model_view`` is the redacted, whole-
rupee projection that crosses the perimeter. Descriptions appear in neither —
raw narration carries account numbers and counterparty names, and no question
worth answering needs it.

**Nothing is left for the model to work out.** Deltas, shares, percentages and
totals are computed here and handed over finished. A model that has to subtract
in order to answer is a model that can subtract wrongly, and no amount of
prompt wording fixes that. This is also what makes the traceability check
strict enough to be worth running: every figure in a good answer is already
present in a tool result, so anything else is invented.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant import period as period_mod
from app.assistant.redaction import (
    business_merchants,
    known_merchants,
    merchant_for_model,
    percent,
    rupees,
    scrub_mentions,
)
from app.intelligence import analytics, anomaly, recurring
from app.privacy.output_validator import VALID_CATEGORIES
from app.services import transactions as txn_service

ZERO = Decimal("0.00")

#: How many ledger rows a single question may pull back. A cap rather than a
#: page: the assistant answers questions, it is not a second ledger screen, and
#: the "open in Transactions" link exists precisely so the full list has a
#: proper home with paging and filters.
MAX_ROWS = 25


def plural(count: int, noun: str, suffix: str = "s") -> str:
    """"1 transaction" / "6 transactions". Small, but a sentence with "1
    transactions" in it reads as machine output and undermines the rest."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}{suffix}"


def inr(value: Any) -> str:
    """Indian digit grouping, whole rupees.

    Whole rupees in prose, exact paise on the card beside it. The prose figure
    is the one a model may quote and a checker must verify, and a single
    canonical rendering is what makes that comparison possible.
    """
    whole = f"{abs(rupees(value)):d}"
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"₹{whole}"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class ToolResult:
    """One tool call's output, in both of its faces."""

    name: str
    arguments: dict[str, Any]
    #: Exact and complete. Rendered by the browser, never sent to a model.
    display: dict[str, Any]
    #: Redacted and rounded. The only thing that crosses the perimeter.
    model_view: dict[str, Any]
    #: A complete answer with no model involved. Used when AI is off, and used
    #: again when the model's answer fails the traceability check.
    headline: str
    #: Which card the UI draws.
    render: str
    #: Ledger filters reproducing this answer, for "open in Transactions".
    filters: dict[str, Any] | None = None
    #: Merchant names withheld from the model view, so the orchestrator can
    #: report honestly that some payees were not named.
    withheld_merchants: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Arguments — no tenant, user or account field exists on any of these
# --------------------------------------------------------------------------- #

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_max_length=80)


class MonthlySpendingArgs(_Args):
    month: str | None = Field(default=None, description="YYYY-MM")


class CategorySpendingArgs(_Args):
    period: str | None = Field(default=None, description="YYYY-MM or YYYY")
    category: str | None = Field(default=None, description="a category slug")


class TransactionsArgs(_Args):
    period: str | None = None
    category: str | None = None
    merchant: str | None = None
    min_amount: int | None = Field(default=None, ge=0)
    max_amount: int | None = Field(default=None, ge=0)
    direction: str | None = None
    limit: int = Field(default=10, ge=1, le=MAX_ROWS)


class TopMerchantsArgs(_Args):
    period: str | None = None
    limit: int = Field(default=10, ge=1, le=MAX_ROWS)


class RecurringArgs(_Args):
    pass


class CompareMonthsArgs(_Args):
    left: str = Field(description="the earlier month, YYYY-MM")
    right: str = Field(description="the later month, YYYY-MM")
    category: str | None = Field(
        default=None, description="narrow the headline to one category slug"
    )


class AnomaliesArgs(_Args):
    limit: int = Field(default=10, ge=1, le=MAX_ROWS)


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #

async def _run_monthly_spending(
    session: AsyncSession, args: MonthlySpendingArgs, default: date
) -> ToolResult:
    month = period_mod.parse_month(args.month, default=default)
    summary = await analytics.monthly_summary(session, month)
    label = period_mod.month_label(month)
    display = summary.as_dict()
    quality = summary.quality

    view = {
        "month": month.strftime("%Y-%m"),
        "month_label": label,
        "spending_rupees": rupees(summary.net_expenses),
        "income_rupees": rupees(summary.income),
        "refunds_rupees": rupees(summary.refunds),
        "net_cash_flow_rupees": rupees(summary.net_cash_flow),
        "savings_rate_percent": percent(summary.savings_rate),
        "transaction_count": summary.transaction_count,
        "expense_transaction_count": summary.expense_transaction_count,
        "figures_include_unverified": not quality.fully_trusted,
        "awaiting_review_count": quality.awaiting_review,
        "from_untrusted_statements_count": quality.from_untrusted_statements,
    }

    headline = (
        f"In {label} you spent {inr(summary.net_expenses)} across "
        f"{plural(summary.expense_transaction_count, 'transaction')}"
        + (
            f", against {inr(summary.income)} of income — "
            f"{percent(summary.savings_rate)}% of it kept."
            if summary.income > ZERO
            else "."
        )
    )

    first, last = analytics.month_bounds(month)
    return ToolResult(
        name="get_monthly_spending",
        arguments=args.model_dump(exclude_none=True),
        display=display,
        model_view=view,
        headline=headline,
        render="summary",
        filters={"date_from": first.isoformat(), "date_to": last.isoformat()},
    )


async def _run_category_spending(
    session: AsyncSession, args: CategorySpendingArgs, default: date
) -> ToolResult:
    window = period_mod.parse(args.period, default=default)
    rows = await analytics.category_breakdown_between(
        session, window.first, window.last, limit=30
    )

    wanted = (args.category or "").strip().lower().replace(" ", "_") or None
    if wanted:
        rows = [row for row in rows if row["slug"] == wanted]

    total = sum((Decimal(row["total"]) for row in rows), ZERO)

    view = {
        "period": args.period or window.first.strftime("%Y-%m"),
        "period_label": window.label,
        "total_rupees": rupees(total),
        "categories": [
            {
                "category": row["name"],
                "amount_rupees": rupees(row["total"]),
                "share_percent": percent(row["share"]),
                "transaction_count": row["transaction_count"],
            }
            for row in rows
        ],
    }

    if not rows:
        headline = (
            f"Nothing was spent on {wanted.replace('_', ' ')} in {window.label}."
            if wanted
            else f"No spending is recorded for {window.label}."
        )
    elif wanted:
        row = rows[0]
        headline = (
            f"You spent {inr(row['total'])} on {row['name']} in {window.label}, "
            f"across {plural(int(row['transaction_count']), 'transaction')}."
        )
    else:
        top = rows[0]
        headline = (
            f"{window.label} spending was {inr(total)}, led by {top['name']} at "
            f"{inr(top['total'])} ({percent(top['share'])}%)."
        )

    return ToolResult(
        name="get_category_spending",
        arguments=args.model_dump(exclude_none=True),
        display={
            "period_label": window.label,
            "total": str(total),
            "categories": rows,
        },
        model_view=view,
        headline=headline,
        render="categories",
        filters={
            "date_from": window.first.isoformat(),
            "date_to": window.last.isoformat(),
            **({"category": wanted} if wanted else {}),
        },
    )


async def _run_transactions(
    session: AsyncSession, args: TransactionsArgs, default: date
) -> ToolResult:
    window = period_mod.parse(args.period, default=default)
    category = (args.category or "").strip().lower().replace(" ", "_") or None

    criteria: dict[str, Any] = {
        "date_from": window.first,
        "date_to": window.last,
        "category_slug": category,
        "merchant_like": args.merchant or None,
        "direction": args.direction if args.direction in {"debit", "credit"} else None,
        "min_amount": Decimal(args.min_amount) if args.min_amount is not None else None,
        "max_amount": Decimal(args.max_amount) if args.max_amount is not None else None,
    }

    totals = await txn_service.sum_transactions(session, **criteria)
    rows, _ = await txn_service.list_transactions(
        session, limit=args.limit, offset=0, **criteria
    )

    # The dictionary alone here: each row carries its own payment method, so
    # the rail test is applied per transaction rather than over a history.
    names = [row["merchant"] for row in rows if row["merchant"]]
    known = await known_merchants(session, names)

    withheld: list[str] = []
    items = []
    for row in rows:
        name = row["merchant"]
        safe = merchant_for_model(
            name, is_known=name in known, payment_method=row["payment_method"]
        )
        if name and safe is None:
            withheld.append(name)
        items.append(
            {
                "date": row["txn_date"].isoformat(),
                "merchant": safe,
                "merchant_withheld": bool(name) and safe is None,
                "amount_rupees": rupees(row["amount"]),
                "direction": row["direction"],
                "category": row["category_name"],
            }
        )

    view = {
        "period_label": window.label,
        "matched_count": totals["matched"],
        "matched_total_rupees": rupees(totals["total"]),
        "returned_count": len(items),
        "transactions": items,
    }

    subject = []
    if args.merchant:
        subject.append(f"at {args.merchant}")
    if category:
        subject.append(f"in {category.replace('_', ' ')}")
    if args.min_amount is not None:
        subject.append(f"above {inr(args.min_amount)}")
    if args.max_amount is not None:
        subject.append(f"below {inr(args.max_amount)}")
    what = " ".join(subject) or "in total"

    headline = (
        f"{plural(totals['matched'], 'transaction')} {what} in {window.label}, "
        f"totalling {inr(totals['total'])}."
        if totals["matched"]
        else f"No transactions {what} in {window.label}."
    )

    return ToolResult(
        name="get_transactions",
        arguments=args.model_dump(exclude_none=True),
        display={
            "period_label": window.label,
            "matched": totals["matched"],
            "total": str(totals["total"]),
            "transactions": [
                {
                    "id": str(row["id"]),
                    "txn_date": row["txn_date"].isoformat(),
                    "merchant": row["merchant"],
                    "amount": str(row["amount"]),
                    "direction": row["direction"],
                    "category_name": row["category_name"],
                    "category_color": row["category_color"],
                }
                for row in rows
            ],
        },
        model_view=view,
        headline=headline,
        render="transactions",
        filters={
            "date_from": window.first.isoformat(),
            "date_to": window.last.isoformat(),
            **({"category": category} if category else {}),
            **({"search": args.merchant} if args.merchant else {}),
            **({"min_amount": str(args.min_amount)} if args.min_amount is not None else {}),
            **({"max_amount": str(args.max_amount)} if args.max_amount is not None else {}),
            **({"direction": args.direction} if args.direction else {}),
        },
        withheld_merchants=withheld,
    )


async def _run_top_merchants(
    session: AsyncSession, args: TopMerchantsArgs, default: date
) -> ToolResult:
    window = period_mod.parse(args.period, default=default)
    rows = await analytics.top_merchants(
        session, None, limit=args.limit, between=window.as_bounds
    )

    # A rollup has no single payment method, so eligibility is decided from the
    # merchant's whole history: dictionary-known, or every payment to it went
    # over a rail an individual cannot be on.
    known = await business_merchants(session, [row["merchant"] for row in rows])

    withheld: list[str] = []
    items = []
    for row in rows:
        safe = merchant_for_model(row["merchant"], is_known=row["merchant"] in known)
        if safe is None:
            withheld.append(row["merchant"])
        items.append(
            {
                "merchant": safe,
                "merchant_withheld": safe is None,
                "total_rupees": rupees(row["total"]),
                "transaction_count": row["transaction_count"],
                "average_rupees": rupees(row["average"]),
            }
        )

    view = {
        "period_label": window.label,
        "merchants": items,
    }

    if rows:
        # The real name, deliberately. A headline is written here and read by
        # the person whose ledger it is — withholding a payee from *them* would
        # be redacting their own statement back at them. The withholding
        # applies to `model_view`, which is the thing that leaves.
        top = rows[0]
        headline = (
            f"Your largest merchant in {window.label} was {top['merchant']}, at "
            f"{inr(top['total'])} over "
            f"{plural(int(top['transaction_count']), 'transaction')}."
        )
    else:
        headline = f"No merchant spending is recorded for {window.label}."

    return ToolResult(
        name="get_top_merchants",
        arguments=args.model_dump(exclude_none=True),
        display={"period_label": window.label, "merchants": rows},
        model_view=view,
        headline=headline,
        render="merchants",
        filters={
            "date_from": window.first.isoformat(),
            "date_to": window.last.isoformat(),
        },
        withheld_merchants=withheld,
    )


async def _run_recurring(
    session: AsyncSession, args: RecurringArgs, default: date
) -> ToolResult:
    rows = await recurring.list_subscriptions(session)
    active = [row for row in rows if str(row["status"]) == "active"]

    known = await business_merchants(session, [row["merchant"] for row in active])
    annual = sum((Decimal(str(row["estimated_annual_cost"])) for row in active), ZERO)

    withheld: list[str] = []
    items = []
    for row in active:
        safe = merchant_for_model(row["merchant"], is_known=row["merchant"] in known)
        if safe is None:
            withheld.append(row["merchant"])
        items.append(
            {
                "merchant": safe,
                "merchant_withheld": safe is None,
                "cadence": str(row["cadence"]),
                "typical_amount_rupees": rupees(row["typical_amount"]),
                "annual_cost_rupees": rupees(row["estimated_annual_cost"]),
                "next_expected_on": (
                    row["next_expected_on"].isoformat()
                    if row["next_expected_on"] else None
                ),
                "charges_seen": row["occurrence_count"],
                "category": row["category_name"],
            }
        )

    view = {
        "count": len(active),
        "annual_total_rupees": rupees(annual),
        "subscriptions": items,
    }

    headline = (
        f"{plural(len(active), 'recurring charge')} active, costing "
        f"{inr(annual)} a year."
        if active
        else "No recurring charges have been detected yet."
    )

    return ToolResult(
        name="get_recurring_expenses",
        arguments={},
        display={"count": len(active), "annual_total": str(annual), "subscriptions": active},
        model_view=view,
        headline=headline,
        render="subscriptions",
        filters=None,
        withheld_merchants=withheld,
    )


async def _run_compare_months(
    session: AsyncSession, args: CompareMonthsArgs, default: date
) -> ToolResult:
    left = period_mod.parse_month(args.left, default=default)
    right = period_mod.parse_month(args.right, default=default)
    comparison = await analytics.compare_months(session, left, right)

    change = Decimal(comparison["expense_change"])
    left_label = period_mod.month_label(left)
    right_label = period_mod.month_label(right)

    movements = [
        row for row in comparison["categories"] if Decimal(row["change"]) != ZERO
    ][:10]

    # An optional focus, not a filter: the whole per-category movement is still
    # returned, because "food went up ₹2,000" is only meaningful next to what
    # everything else did.
    wanted = (args.category or "").strip().lower().replace(" ", "_") or None
    focus = next(
        (row for row in comparison["categories"] if row["slug"] == wanted), None
    ) if wanted else None

    view = {
        "earlier_month": left.strftime("%Y-%m"),
        "earlier_label": left_label,
        "later_month": right.strftime("%Y-%m"),
        "later_label": right_label,
        "earlier_spending_rupees": rupees(comparison["left"]["net_expenses"]),
        "later_spending_rupees": rupees(comparison["right"]["net_expenses"]),
        # Pre-computed. The model is never asked to subtract two figures it can
        # see — that is the arithmetic it is not allowed to do.
        "change_rupees": rupees(abs(change)),
        "change_direction": (
            "increase" if change > ZERO else "decrease" if change < ZERO else "unchanged"
        ),
        "categories": [
            {
                "category": row["name"],
                "before_rupees": rupees(row["before"]),
                "after_rupees": rupees(row["after"]),
                "change_rupees": rupees(abs(Decimal(row["change"]))),
                "change_direction": (
                    "increase" if Decimal(row["change"]) > ZERO else "decrease"
                ),
            }
            for row in movements
        ],
    }
    if focus is not None:
        view["focus_category"] = focus["name"]

    if focus is not None:
        focus_change = Decimal(focus["change"])
        if focus_change == ZERO:
            headline = (
                f"{focus['name']} spending was unchanged between {left_label} and "
                f"{right_label}, at {inr(focus['after'])}."
            )
        else:
            word = "more" if focus_change > ZERO else "less"
            moved = "up" if focus_change > ZERO else "down"
            headline = (
                f"You spent {inr(abs(focus_change))} {word} on {focus['name']} in "
                f"{right_label} than in {left_label} — {moved} from "
                f"{inr(focus['before'])} to {inr(focus['after'])}."
            )
    elif change == ZERO:
        headline = f"Spending was unchanged between {left_label} and {right_label}."
    else:
        word = "more" if change > ZERO else "less"
        biggest = movements[0] if movements else None
        headline = (
            f"You spent {inr(abs(change))} {word} in {right_label} than in "
            f"{left_label}"
            + (
                f", with {biggest['name']} moving the most at "
                f"{inr(abs(Decimal(biggest['change'])))}."
                if biggest else "."
            )
        )

    return ToolResult(
        name="compare_months",
        arguments=args.model_dump(exclude_none=True),
        display={
            "earlier_label": left_label,
            "later_label": right_label,
            "expense_change": comparison["expense_change"],
            "left": comparison["left"],
            "right": comparison["right"],
            "categories": movements,
        },
        model_view=view,
        headline=headline,
        render="comparison",
        filters={
            "date_from": analytics.month_bounds(right)[0].isoformat(),
            "date_to": analytics.month_bounds(right)[1].isoformat(),
        },
    )


async def _run_anomalies(
    session: AsyncSession, args: AnomaliesArgs, default: date
) -> ToolResult:
    rows = await anomaly.list_anomalies(session, limit=args.limit)

    names = [row["merchant"] for row in rows if row["merchant"]]
    known = await business_merchants(session, names)

    withheld: list[str] = []
    items = []
    for row in rows:
        name = row["merchant"]
        safe = merchant_for_model(name, is_known=name in known) if name else None
        if name and safe is None:
            withheld.append(name)
        items.append(
            {
                "kind": str(row["kind"]),
                "date": row["detected_on"].isoformat(),
                "merchant": safe,
                "merchant_withheld": bool(name) and safe is None,
                "observed_rupees": (
                    rupees(row["observed_value"]) if row["observed_value"] is not None
                    else None
                ),
                "usual_rupees": (
                    rupees(row["baseline_value"]) if row["baseline_value"] is not None
                    else None
                ),
                # The reason is assembled deterministically from a name and two
                # numbers. When the name may not be sent, the numbers still can.
                "reason": scrub_mentions(row["reason"], withheld=[name] if name and safe is None else []),
                "category": row["category_name"],
            }
        )

    view = {"count": len(items), "outliers": items}

    # Quotes the engine's own reason verbatim rather than re-wording it: that
    # sentence was assembled from the figures that triggered the detection, and
    # paraphrasing it here would be a second place for the explanation to live.
    if rows:
        verb = "stands out" if len(rows) == 1 else "stand out"
        headline = (
            f"{plural(len(rows), 'transaction')} {verb} statistically. "
            + rows[0]["reason"]
        )
    else:
        headline = "Nothing stands out statistically."

    return ToolResult(
        name="get_anomalies",
        arguments=args.model_dump(exclude_none=True),
        display={"count": len(rows), "anomalies": rows},
        model_view=view,
        headline=headline.strip(),
        render="anomalies",
        filters=None,
        withheld_merchants=withheld,
    )


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    #: The declaration handed to the provider. Written by hand rather than
    #: generated from the Pydantic model: a generated schema for `str | None`
    #: comes out as an `anyOf`, which several providers reject outright, and a
    #: schema the model cannot read is a tool the model will not call.
    parameters: dict[str, Any]
    runner: Callable[[AsyncSession, Any, date], Awaitable[ToolResult]]


_PERIOD = {
    "type": "STRING",
    "description": "A month as YYYY-MM, or a whole year as YYYY. Omit for the current month.",
}

REGISTRY: tuple[Tool, ...] = (
    Tool(
        name="get_monthly_spending",
        description=(
            "Headline figures for one month: total spending, income, net cash "
            "flow, savings rate and transaction counts."
        ),
        args_model=MonthlySpendingArgs,
        parameters={
            "type": "OBJECT",
            "properties": {
                "month": {"type": "STRING", "description": "The month as YYYY-MM."}
            },
        },
        runner=_run_monthly_spending,
    ),
    Tool(
        name="get_category_spending",
        description=(
            "Spending broken down by category for a month or a year. Pass a "
            "category slug to get just that one."
        ),
        args_model=CategorySpendingArgs,
        parameters={
            "type": "OBJECT",
            "properties": {
                "period": _PERIOD,
                "category": {
                    "type": "STRING",
                    "enum": sorted(VALID_CATEGORIES),
                    "description": "Restrict to a single category.",
                },
            },
        },
        runner=_run_category_spending,
    ),
    Tool(
        name="get_transactions",
        description=(
            "Individual transactions matching a filter, with the count and "
            "total for the whole match — not only the rows returned."
        ),
        args_model=TransactionsArgs,
        parameters={
            "type": "OBJECT",
            "properties": {
                "period": _PERIOD,
                "category": {"type": "STRING", "enum": sorted(VALID_CATEGORIES)},
                "merchant": {
                    "type": "STRING",
                    "description": "Match merchants containing this text.",
                },
                "min_amount": {"type": "INTEGER", "description": "Rupees, inclusive."},
                "max_amount": {"type": "INTEGER", "description": "Rupees, inclusive."},
                "direction": {"type": "STRING", "enum": ["debit", "credit"]},
                "limit": {"type": "INTEGER", "description": f"1 to {MAX_ROWS}."},
            },
        },
        runner=_run_transactions,
    ),
    Tool(
        name="get_top_merchants",
        description="The merchants the most money went to, largest first.",
        args_model=TopMerchantsArgs,
        parameters={
            "type": "OBJECT",
            "properties": {
                "period": _PERIOD,
                "limit": {"type": "INTEGER", "description": f"1 to {MAX_ROWS}."},
            },
        },
        runner=_run_top_merchants,
    ),
    Tool(
        name="get_recurring_expenses",
        description=(
            "Detected subscriptions and other recurring charges, with cadence, "
            "typical amount, next expected date and annual cost."
        ),
        args_model=RecurringArgs,
        parameters={"type": "OBJECT", "properties": {}},
        runner=_run_recurring,
    ),
    Tool(
        name="compare_months",
        description=(
            "Two months side by side, with the change already computed overall "
            "and per category."
        ),
        args_model=CompareMonthsArgs,
        parameters={
            "type": "OBJECT",
            "properties": {
                "left": {"type": "STRING", "description": "The earlier month, YYYY-MM."},
                "right": {"type": "STRING", "description": "The later month, YYYY-MM."},
                "category": {
                    "type": "STRING",
                    "enum": sorted(VALID_CATEGORIES),
                    "description": "Optional: lead the answer with this category.",
                },
            },
            "required": ["left", "right"],
        },
        runner=_run_compare_months,
    ),
    Tool(
        name="get_anomalies",
        description=(
            "Transactions that are statistical outliers, each with the observed "
            "figure, the usual figure and a stated reason. These are outliers, "
            "not fraud findings."
        ),
        args_model=AnomaliesArgs,
        parameters={
            "type": "OBJECT",
            "properties": {"limit": {"type": "INTEGER", "description": f"1 to {MAX_ROWS}."}},
        },
        runner=_run_anomalies,
    ),
)

BY_NAME: dict[str, Tool] = {tool.name: tool for tool in REGISTRY}

TOOL_NAMES: tuple[str, ...] = tuple(tool.name for tool in REGISTRY)


def declarations() -> list[dict[str, Any]]:
    """The function declarations handed to a provider."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in REGISTRY
    ]
