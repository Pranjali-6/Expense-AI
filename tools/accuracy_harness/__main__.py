"""Score the parsers against ground truth and gate the phase on the result.

    make accuracy         # synthetic golden fixtures
    make validate-real    # your own redacted statements (P4.5)

Prints a per-fixture and per-bank scorecard with **absolute counts alongside
percentages** — "3 missing transactions in HDFC-Mar-2024" is actionable and
"99.4%" is not — writes an ``extraction_accuracy_runs`` row per fixture, and
exits non-zero if any target is missed.

This is a gate, not a report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from app.models.enums import AccuracyCorpus, DocumentType

from tools.accuracy_harness import targets as target_spec
from tools.accuracy_harness.scoring import (
    GroundTruth,
    Scorecard,
    reconcile_bank,
    reconcile_card,
    score,
)

SYNTHETIC_DIR = Path("/app/tests/fixtures/statements")
REAL_DIR = Path("/app/tests/fixtures/real")

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if _supports_colour() else text


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd="/app", stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Running one fixture
# --------------------------------------------------------------------------- #

def expected_path(pdf_path: Path) -> Path:
    """``hdfc-2024-03.pdf`` → ``hdfc-2024-03.expected.json``.

    Built by name rather than with ``Path.with_suffix``, which treats every dot
    as a suffix boundary and would turn ``hdfc-2024-03`` into ``hdfc-2024``.
    """
    return pdf_path.with_name(f"{pdf_path.stem}.expected.json")


def score_fixture(pdf_path: Path) -> Scorecard:
    from app.extraction.pipeline import parse_document

    truth = json.loads(expected_path(pdf_path).read_text())

    outcome = parse_document(pdf_path.read_bytes())
    result = outcome.result
    metadata = truth["metadata"]

    card = score(
        fixture=truth["fixture"],
        bank_code=metadata["bank_code"],
        document_type=metadata["document_type"],
        expected=[GroundTruth.from_json(row) for row in truth["transactions"]],
        extracted=result.transactions,
    )

    # Reconciliation uses the *parser's own* metadata, not the fixture's. Scoring
    # it against the generator's numbers would only prove the generator can add
    # up; the question is whether the parser read a statement that closes.
    if result.metadata.document_type == DocumentType.CREDIT_CARD_STATEMENT:
        reconciles, delta = reconcile_card(
            result.metadata.opening_balance,
            result.metadata.total_amount_due,
            result.transactions,
        )
    else:
        reconciles, delta = reconcile_bank(
            result.metadata.opening_balance,
            result.metadata.closing_balance,
            result.transactions,
        )

    card.reconciles = reconciles
    card.reconciliation_delta = delta
    card.reconciliation_checked = delta is not None
    return card


def aggregate(cards: list[Scorecard], *, fixture: str, bank_code: str | None) -> Scorecard:
    total = Scorecard(fixture=fixture, bank_code=bank_code or "ALL", document_type="all")
    for card in cards:
        total.expected_count += card.expected_count
        total.extracted_count += card.extracted_count
        total.matched_count += card.matched_count
        total.missing += card.missing
        total.extra += card.extra
        total.errors.date += card.errors.date
        total.errors.amount += card.errors.amount
        total.errors.direction += card.errors.direction
        total.errors.merchant += card.errors.merchant
        total.errors.category += card.errors.category
    total.reconciles = all(card.reconciles for card in cards)
    total.reconciliation_checked = all(card.reconciliation_checked for card in cards)
    return total


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _row(card: Scorecard, *, width: int = 26) -> str:
    recon = (
        _paint("  ✓ ", GREEN) if card.reconciles
        else _paint("  ✗ ", RED) if card.reconciliation_checked
        else _paint("  ? ", YELLOW)
    )
    return (
        f"  {card.fixture:<{width}} {card.expected_count:>4} {card.extracted_count:>5} "
        f"{card.missing:>4} {card.extra:>4} {card.errors.date:>4} {card.errors.amount:>4} "
        f"{card.errors.direction:>4} {card.errors.merchant:>4} {card.errors.category:>4} "
        f"{recon}"
    )


def print_report(cards: list[Scorecard], overall: Scorecard, corpus: str) -> bool:
    print()
    print(_paint(f"  Extraction accuracy — corpus: {corpus}", BOLD))
    print()
    print(_paint(
        f"  {'fixture':<26} {'exp':>4} {'extr':>5} {'miss':>4} {'xtra':>4} "
        f"{'date':>4} {'amt':>4} {'dir':>4} {'mrch':>4} {'cat':>4}   recon", DIM
    ))
    print(_paint("  " + "─" * 82, DIM))
    for card in sorted(cards, key=lambda item: item.fixture):
        print(_row(card))

    by_bank: dict[str, list[Scorecard]] = defaultdict(list)
    for card in cards:
        by_bank[card.bank_code].append(card)

    if len(by_bank) > 1:
        print()
        print(_paint("  per bank", DIM))
        print(_paint("  " + "─" * 82, DIM))
        for bank in sorted(by_bank):
            print(_row(aggregate(by_bank[bank], fixture=bank, bank_code=bank)))

    print()
    print(_paint("  " + "─" * 82, DIM))
    print(_row(overall))

    # --- targets -------------------------------------------------------------
    print()
    print(_paint("  Targets", BOLD))
    print()
    passed = True
    for target in target_spec.TARGETS:
        measured = getattr(overall, target.key)
        ok = measured >= target.minimum
        passed = passed and ok
        mark = _paint("PASS", GREEN) if ok else _paint("FAIL", RED)
        print(
            f"  {target.label:<26} {measured * 100:7.3f}%   "
            f"target {target.minimum * 100:6.2f}%   {mark}"
        )

    unreconciled = [card for card in cards if not card.reconciles]
    recon_ok = not unreconciled
    passed = passed and recon_ok
    mark = _paint("PASS", GREEN) if recon_ok else _paint("FAIL", RED)
    print(
        f"  {'Financial reconciliation':<26} "
        f"{len(cards) - len(unreconciled):>3}/{len(cards):<3} statements    "
        f"target    100%   {mark}"
    )

    if unreconciled:
        print()
        print(_paint("  statements that do not reconcile", RED))
        for card in unreconciled:
            delta = (
                f"delta ₹{card.reconciliation_delta}"
                if card.reconciliation_delta is not None
                else "not checkable — the statement did not print both balances"
            )
            print(f"    {card.fixture:<28} {delta}")

    failures = [card for card in cards if card.examples and (
        card.missing or card.extra or card.errors.date or card.errors.amount
        or card.errors.direction
    )]
    if failures:
        print()
        print(_paint("  first failures", YELLOW))
        for card in failures[:6]:
            print(f"    {card.fixture}")
            for example in card.examples[:4]:
                print(f"      · {example}")

    print()
    print(_paint("  " + ("ACCURACY GATE PASSED" if passed else "ACCURACY GATE FAILED"),
                 GREEN if passed else RED))
    print()
    return passed


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

async def record(cards: list[Scorecard], overall: Scorecard, *, corpus: str,
                 passed: bool) -> None:
    """Write one row per fixture plus an aggregate, so regressions are visible."""
    from sqlalchemy import text as sql

    from app.db.session import dispose_engine, get_session_factory

    commit = _git_commit()
    factory = get_session_factory()

    statement = sql(
        """
        INSERT INTO extraction_accuracy_runs (
            corpus, bank_code, fixture_name, parser_version, git_commit,
            expected_count, extracted_count, matched_count,
            missing_transactions, extra_transactions,
            wrong_date, wrong_amount, wrong_direction, wrong_merchant, wrong_category,
            recall, precision, date_accuracy, amount_accuracy, direction_accuracy,
            merchant_accuracy, category_accuracy, reconciled, passed, detail
        ) VALUES (
            :corpus, :bank_code, :fixture_name, :parser_version, :git_commit,
            :expected_count, :extracted_count, :matched_count,
            :missing_transactions, :extra_transactions,
            :wrong_date, :wrong_amount, :wrong_direction, :wrong_merchant, :wrong_category,
            :recall, :precision, :date_accuracy, :amount_accuracy, :direction_accuracy,
            :merchant_accuracy, :category_accuracy, :reconciled, :passed,
            CAST(:detail AS jsonb)
        )
        """
    )

    async with factory() as session:
        for card in [*cards, overall]:
            payload = card.as_dict()
            is_aggregate = card is overall
            await session.execute(
                statement,
                {
                    "corpus": corpus,
                    "bank_code": None if is_aggregate else card.bank_code,
                    "fixture_name": None if is_aggregate else card.fixture,
                    "parser_version": "1.0",
                    "git_commit": commit,
                    "expected_count": card.expected_count,
                    "extracted_count": card.extracted_count,
                    "matched_count": card.matched_count,
                    "missing_transactions": card.missing,
                    "extra_transactions": card.extra,
                    "wrong_date": card.errors.date,
                    "wrong_amount": card.errors.amount,
                    "wrong_direction": card.errors.direction,
                    "wrong_merchant": card.errors.merchant,
                    "wrong_category": card.errors.category,
                    "recall": Decimal(str(payload["recall"])),
                    "precision": Decimal(str(payload["precision"])),
                    "date_accuracy": Decimal(str(payload["date_accuracy"])),
                    "amount_accuracy": Decimal(str(payload["amount_accuracy"])),
                    "direction_accuracy": Decimal(str(payload["direction_accuracy"])),
                    "merchant_accuracy": Decimal(str(payload["merchant_accuracy"])),
                    "category_accuracy": Decimal(str(payload["category_accuracy"])),
                    "reconciled": card.reconciles,
                    "passed": passed if is_aggregate else card.reconciles,
                    "detail": json.dumps(payload),
                },
            )
        await session.commit()

    await dispose_engine()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", choices=("synthetic", "real"), default="synthetic")
    parser.add_argument("--fixtures", type=Path, default=None)
    parser.add_argument("--no-record", action="store_true",
                        help="skip writing extraction_accuracy_runs rows")
    args = parser.parse_args()

    directory = args.fixtures or (
        SYNTHETIC_DIR if args.corpus == "synthetic" else REAL_DIR
    )
    pdfs = sorted(directory.glob("*.pdf")) if directory.exists() else []

    if not pdfs:
        if args.corpus == "real":
            # Deliberately not a failure and deliberately not a pass. There is
            # no corpus, so there is nothing to claim.
            print()
            print(_paint("  Real-statement validation — no corpus supplied", YELLOW))
            print()
            print("  The machinery is in place and runs the moment you add statements.")
            print(f"  Drop redacted PDFs and their expected.json into {REAL_DIR} and re-run.")
            print("  Until then this reports no result rather than a false green:")
            print("  synthetic accuracy says the framework is correct against layouts")
            print("  we authored, which is not the same as saying it reads your bank.")
            print()
            return 0
        print(_paint(f"\n  No fixtures in {directory}. Run `make gen-fixtures` first.\n", RED))
        return 1

    cards = [score_fixture(path) for path in pdfs]
    overall = aggregate(cards, fixture="TOTAL", bank_code=None)
    passed = print_report(cards, overall, args.corpus)

    if not args.no_record:
        try:
            asyncio.run(record(cards, overall, corpus=str(AccuracyCorpus(args.corpus)),
                               passed=passed))
        except Exception as exc:
            # A scorecard that cannot be filed is still a valid scorecard. The
            # gate decision must not depend on the database being reachable.
            print(_paint(f"  (accuracy history not recorded: {type(exc).__name__})\n", DIM))

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
