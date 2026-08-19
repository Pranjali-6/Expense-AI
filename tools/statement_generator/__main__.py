"""Generate the golden fixture set: statement PDFs paired with ground truth.

Each fixture is two files sharing a stem — ``hdfc-2024-03.pdf`` and
``hdfc-2024-03.expected.json``. The JSON is written from the same in-memory
ledger the PDF was rendered from, so it is the document's *source*, not a
transcription of it. That distinction is the whole point: a hand-transcribed
expectation can be wrong in exactly the way the parser is wrong, and then the
fixture agrees with the bug.

The set deliberately includes layouts built to break parsers — a single amount
column carrying Dr/Cr, lakh-grouped six-figure amounts, brought-forward lines on
every continuation page, a credit-card statement whose refunds are credits, and
a scanned copy with no text layer at all.

Run:  make gen-fixtures
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.models.enums import Direction

from tools.statement_generator import spec as specs
from tools.statement_generator.ledger import (
    GeneratedStatement,
    build_card_statement,
    build_statement,
)
from tools.statement_generator.render import rasterise, render

GENERATOR_VERSION = "1"
DEFAULT_OUTPUT = Path("/app/tests/fixtures/statements")


def _ground_truth(statement: GeneratedStatement, *, name: str, seed: int,
                  page_count: int, notes: dict) -> dict:
    """Serialise the ledger as the expected result.

    Money is written as a **string**. JSON numbers are doubles, and a fixture
    that cannot round-trip ₹1,23,456.78 exactly is not a fixture worth having.
    """
    debits = sum(
        (entry.amount for entry in statement.entries if entry.direction == Direction.DEBIT),
        Decimal("0.00"),
    )
    credits = sum(
        (entry.amount for entry in statement.entries if entry.direction == Direction.CREDIT),
        Decimal("0.00"),
    )

    return {
        "fixture": name,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "page_count": page_count,
        "notes": notes,
        "metadata": {
            "bank_code": statement.spec.code,
            "bank_name": statement.spec.name,
            "document_type": str(statement.spec.document_type),
            "account_last4": statement.account_last4,
            "period_start": statement.period_start.isoformat(),
            "period_end": statement.period_end.isoformat(),
            "opening_balance": str(statement.opening_balance),
            "closing_balance": str(statement.closing_balance),
            "declared_transaction_count": (
                len(statement.entries) if statement.spec.prints_transaction_count else None
            ),
            "total_amount_due": str(statement.total_due) if statement.total_due is not None else None,
            "minimum_amount_due": str(statement.minimum_due) if statement.minimum_due is not None else None,
            "payment_due_date": (
                statement.payment_due_date.isoformat() if statement.payment_due_date else None
            ),
            "credit_limit": str(statement.credit_limit) if statement.credit_limit is not None else None,
        },
        "reconciliation": {
            "total_debits": str(debits),
            "total_credits": str(credits),
            # The identity every bank statement must satisfy. Written out so the
            # harness checks the parser's arithmetic against the generator's,
            # rather than against its own re-derivation of the same sum.
            "expected_closing": str(
                statement.opening_balance - debits + credits
                if statement.total_due is None
                else statement.opening_balance + debits - credits
            ),
        },
        "transactions": [
            {
                "txn_date": entry.txn_date.isoformat(),
                "value_date": entry.value_date.isoformat(),
                "description": entry.description,
                "amount": str(entry.amount),
                "direction": str(entry.direction),
                "balance_after": (
                    str(entry.balance_after) if statement.total_due is None else None
                ),
                "reference": entry.reference or None,
                "merchant_normalized": entry.merchant,
                "merchant_slug": entry.merchant_slug,
                "payment_method": str(entry.payment_method),
                "category_slug": entry.category_slug,
                "subcategory_slug": entry.subcategory_slug,
            }
            for entry in statement.entries
        ],
    }


def _write(output: Path, name: str, statement: GeneratedStatement, *, seed: int,
           rows_per_page: int = 26, scanned: bool = False, notes: dict | None = None) -> dict:
    pdf_path = output / f"{name}.pdf"
    page_count = render(statement, pdf_path, rows_per_page=rows_per_page)

    if scanned:
        rasterise(pdf_path, pdf_path)

    truth = _ground_truth(
        statement, name=name, seed=seed, page_count=page_count,
        notes={**(notes or {}), "scanned": scanned},
    )
    (output / f"{name}.expected.json").write_text(json.dumps(truth, indent=2) + "\n")
    return truth


# --------------------------------------------------------------------------- #
# The fixture set
# --------------------------------------------------------------------------- #

MARCH = (date(2024, 3, 1), date(2024, 3, 31))
APRIL = (date(2024, 4, 1), date(2024, 4, 30))


def generate(output: Path, *, include_scanned: bool = True) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []

    plain_banks = (
        ("hdfc", specs.HDFC, 4711, Decimal("184320.55"), 0),
        ("icici", specs.ICICI, 4712, Decimal("96450.20"), 1),
        ("sbi", specs.SBI, 4713, Decimal("237810.00"), 2),
        ("axis", specs.AXIS, 4714, Decimal("58990.75"), 3),
        ("kotak", specs.KOTAK, 4715, Decimal("142300.40"), 0),
        ("idfc", specs.IDFC, 4716, Decimal("73215.90"), 1),
        ("indusind", specs.INDUSIND, 4717, Decimal("119400.10"), 2),
        ("yes", specs.YESBANK, 4718, Decimal("64850.30"), 3),
        ("generic", specs.GENERIC_BANK, 4719, Decimal("88120.65"), 0),
    )

    for slug, bank, seed, opening, holder in plain_banks:
        statement = build_statement(
            bank, seed=seed, period_start=MARCH[0], period_end=MARCH[1],
            opening_balance=opening, holder_index=holder,
        )
        written.append(_write(
            output, f"{slug}-2024-03", statement, seed=seed,
            notes={"purpose": "baseline layout"},
        ))

    # --- credit cards --------------------------------------------------------
    for slug, bank, seed, holder in (
        ("hdfc-card", specs.HDFC_CARD, 4731, 0),
        ("icici-card", specs.ICICI_CARD, 4732, 2),
    ):
        statement = build_card_statement(
            bank, seed=seed, period_start=MARCH[0], period_end=MARCH[1],
            holder_index=holder,
        )
        written.append(_write(
            output, f"{slug}-2024-03", statement, seed=seed,
            notes={"purpose": "credit card; refunds print as Cr and must not "
                              "be counted as spending"},
        ))

    # --- hostile variants ----------------------------------------------------

    # Six-figure amounts, so lakh grouping (1,23,456.78) is exercised on every
    # column rather than only on the balance.
    lakh = build_statement(
        specs.HDFC, seed=4741, period_start=APRIL[0], period_end=APRIL[1],
        opening_balance=Decimal("1875430.25"), holder_index=1, lakh_scale=True,
    )
    written.append(_write(
        output, "hdfc-2024-04-lakh", lakh, seed=4741,
        notes={"purpose": "lakh/crore digit grouping"},
    ))

    # Deliberately short pages, so a 60-row statement spans five pages and the
    # B/F/C/F lines appear four times.
    multipage = build_statement(
        specs.AXIS, seed=4742, period_start=APRIL[0], period_end=APRIL[1],
        opening_balance=Decimal("312450.80"), holder_index=3,
    )
    written.append(_write(
        output, "axis-2024-04-multipage", multipage, seed=4742, rows_per_page=11,
        notes={"purpose": "page breaks with brought/carried-forward lines"},
    ))

    # The same statement re-issued: byte-different PDF, identical transactions.
    # P5's deduplication has to produce zero new rows from this.
    reissue = build_statement(
        specs.HDFC, seed=4711, period_start=MARCH[0], period_end=MARCH[1],
        opening_balance=Decimal("184320.55"), holder_index=0,
    )
    written.append(_write(
        output, "hdfc-2024-03-reissued", reissue, seed=4711, rows_per_page=18,
        notes={"purpose": "re-issued copy of hdfc-2024-03; duplicate detection "
                          "must produce zero new transactions"},
    ))

    if include_scanned:
        scanned = build_statement(
            specs.SBI, seed=4743, period_start=APRIL[0], period_end=APRIL[1],
            opening_balance=Decimal("145200.00"), holder_index=2,
        )
        written.append(_write(
            output, "sbi-2024-04-scanned", scanned, seed=4743, scanned=True,
            notes={"purpose": "no text layer; forces the OCR path"},
        ))

    return written


# --------------------------------------------------------------------------- #
# The demo series
# --------------------------------------------------------------------------- #
#
# Separate from the golden fixtures and for a different job. The fixtures exist
# to be *scored* — two months across nine banks, plus every hostile variant we
# could think of. This exists to be *lived in*: consecutive months on one
# account, so trends have a shape, subscriptions reach the three occurrences
# their detector requires, and month-on-month comparison has something to
# compare.
#
# Generated rather than committed, and generated the same way as everything
# else — the demo ledger is produced by the real pipeline reading real PDFs, not
# injected around it. A demo whose data arrived by a private route proves
# nothing about the product.


def _add_months(anchor: date, months: int) -> date:
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def generate_demo(output: Path, *, months: int = 6, ending: date | None = None) -> list[dict]:
    """Consecutive monthly statements for one savings account and one card.

    The opening balance of each month is carried from the last, so the series
    reconciles as a continuous history rather than as N unrelated statements
    that happen to be adjacent.
    """
    output.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []

    last = (ending or date.today().replace(day=1)) - timedelta(days=1)
    first_month = _add_months(last.replace(day=1), -(months - 1))

    balance = Decimal("214500.00")

    # One household, one job. Pinned rather than redrawn each month for two
    # reasons: people do not change employer monthly, and a salary that moves
    # randomly makes every month-on-month comparison a comparison of noise.
    # The figure clears the series' own spending, so the demo shows a household
    # living within its means rather than an implausible 0% savings rate.
    salary = Decimal("225000.00")
    employer = ("KAVERI SYSTEMS LTD", "Kaveri Systems")

    # One savings account and one card, held for the whole series. Unpinned,
    # each month's seed draws a fresh account number, the pipeline resolves
    # each statement to a *different* account, and six months of one household
    # arrives as twelve unrelated accounts — with no balance continuity and
    # nothing for a trend to be a trend of.
    account_number = "50100294770794"
    card_number = "4726XXXXXXXX1015"

    for index in range(months):
        start = _add_months(first_month, index)
        end = _add_months(start, 1) - timedelta(days=1)
        seed = 9100 + index

        statement = build_statement(
            specs.HDFC, seed=seed, period_start=start, period_end=end,
            opening_balance=balance, holder_index=0,
            salary=salary, employer=employer, account_number=account_number,
        )
        written.append(_write(
            output, f"demo-hdfc-{start:%Y-%m}", statement, seed=seed,
            notes={"purpose": "demo series"},
        ))
        balance = Decimal(str(statement.closing_balance))

        card = build_card_statement(
            specs.ICICI_CARD, seed=9200 + index,
            period_start=start, period_end=end, holder_index=0,
            card_number=card_number,
        )
        written.append(_write(
            output, f"demo-card-{start:%Y-%m}", card, seed=9200 + index,
            notes={"purpose": "demo series"},
        ))

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--no-scanned", action="store_true",
        help="skip the rasterised fixture (it is slow to generate and to score)",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="generate the consecutive-month demo series instead of the fixtures",
    )
    parser.add_argument(
        "--months", type=int, default=6, help="months in the demo series",
    )
    args = parser.parse_args()

    if args.demo:
        written = generate_demo(args.output, months=args.months)
        total_rows = sum(len(item["transactions"]) for item in written)
        print(
            f"\n  {len(written)} demo statements · {total_rows} transactions "
            f"→ {args.output}\n"
        )
        for item in written:
            print(
                f"  {item['fixture']:24} {len(item['transactions']):3} txns  "
                f"{item['metadata']['period_start']} – {item['metadata']['period_end']}"
            )
        print()
        return 0

    written = generate(args.output, include_scanned=not args.no_scanned)

    total_rows = sum(len(item["transactions"]) for item in written)
    print(f"\n  {len(written)} fixtures · {total_rows} transactions → {args.output}\n")
    for item in written:
        meta = item["metadata"]
        print(
            f"  {item['fixture']:28} {meta['bank_code']:9} "
            f"{len(item['transactions']):3} txns  {item['page_count']} pages  "
            f"{item['notes'].get('purpose', '')}"
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
