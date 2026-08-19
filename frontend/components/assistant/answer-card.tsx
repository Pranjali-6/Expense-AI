"use client";

/**
 * Drawing a tool result.
 *
 * Every card renders the **display** figures — exact to the paisa, with real
 * payee names. The redaction that governs what a model may see does not apply
 * here and should not: the reader is the person whose statements these are,
 * and showing them a redacted version of their own ledger would be theatre.
 *
 * Each card that has a ledger equivalent carries an "Open in Transactions"
 * link built from the same filters the tool ran with, so any answer can be
 * checked against the rows behind it in one click. That link is the real
 * accountability mechanism — more than any badge, it lets a reader verify the
 * sentence rather than trust it.
 */

import Link from "next/link";
import { ArrowUpRight } from "lucide-react";

import { Money } from "@/components/shared/money";
import { CategoryBars } from "@/components/charts";
import { Badge } from "@/components/ui/badge";
import { formatDate } from "@/lib/format";
import type { AnswerCard as Card } from "@/lib/api";

function transactionsHref(filters: Record<string, string> | null): string | null {
  if (!filters) return null;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }
  return query.size ? `/transactions?${query.toString()}` : null;
}

function Row({
  label,
  detail,
  amount,
  direction,
}: {
  label: string;
  detail?: string;
  amount: string;
  direction?: "debit" | "credit";
}) {
  return (
    <li className="flex items-center gap-3 py-2 text-sm">
      <span className="min-w-0 flex-1">
        <span className="block truncate">{label}</span>
        {detail && <span className="block text-xs text-muted">{detail}</span>}
      </span>
      <Money value={amount} direction={direction} className="text-sm" />
    </li>
  );
}

export function AnswerCardView({ card }: { card: Card }) {
  const href = transactionsHref(card.filters);

  return (
    <div className="rounded-lg border border-border bg-surface-sunken/40 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <Badge variant="outline">{card.tool.replace(/_/g, " ")}</Badge>
        {href && (
          <Link
            href={href}
            className="inline-flex items-center gap-1 text-xs text-primary-text hover:underline"
          >
            Open in Transactions
            <ArrowUpRight className="size-3" aria-hidden="true" />
          </Link>
        )}
      </div>

      <Body card={card} />
    </div>
  );
}

function Body({ card }: { card: Card }) {
  switch (card.render) {
    case "summary": {
      const data = card.data;
      return (
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {[
            ["Spending", data.net_expenses],
            ["Income", data.income],
            ["Net", data.net_cash_flow],
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
              <dd className="mt-1">
                <Money value={value} emphasis="strong" className="text-sm" />
              </dd>
            </div>
          ))}
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted">Savings rate</dt>
            <dd data-slot="amount" className="mt-1 text-sm font-semibold">
              {Math.round(Number(data.savings_rate) * 100)}%
            </dd>
          </div>
        </dl>
      );
    }

    case "categories":
      return card.data.categories.length ? (
        <CategoryBars data={card.data.categories} limit={8} />
      ) : (
        <p className="text-sm text-muted">Nothing in this category.</p>
      );

    case "transactions":
      return card.data.transactions.length ? (
        <ul className="divide-y divide-border">
          {card.data.transactions.map((row) => (
            <Row
              key={row.id}
              label={row.merchant ?? "Unnamed payee"}
              detail={`${formatDate(row.txn_date)}${
                row.category_name ? ` · ${row.category_name}` : ""
              }`}
              amount={row.amount}
              direction={row.direction}
            />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">No matching transactions.</p>
      );

    case "merchants":
      return (
        <ul className="divide-y divide-border">
          {card.data.merchants.map((row) => (
            <Row
              key={row.merchant}
              label={row.merchant}
              detail={`${row.transaction_count}× · last ${formatDate(row.last_seen)}`}
              amount={row.total}
            />
          ))}
        </ul>
      );

    case "subscriptions":
      return (
        <ul className="divide-y divide-border">
          {card.data.subscriptions.map((row) => (
            <Row
              key={row.id}
              label={row.merchant}
              detail={`${row.cadence.replace(/_/g, " ")}${
                row.next_expected_on ? ` · next ${formatDate(row.next_expected_on)}` : ""
              }`}
              amount={row.typical_amount}
            />
          ))}
        </ul>
      );

    case "comparison": {
      const { earlier_label, later_label, categories } = card.data;
      return (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-muted">
              <tr>
                <th scope="col" className="py-1.5 font-medium">Category</th>
                <th scope="col" className="py-1.5 text-right font-medium">{earlier_label}</th>
                <th scope="col" className="py-1.5 text-right font-medium">{later_label}</th>
                <th scope="col" className="py-1.5 text-right font-medium">Change</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {categories.map((row) => (
                <tr key={row.slug}>
                  <td className="py-2">{row.name}</td>
                  <td data-slot="amount" className="py-2 text-right text-muted">
                    <Money value={row.before} className="text-sm" />
                  </td>
                  <td data-slot="amount" className="py-2 text-right">
                    <Money value={row.after} className="text-sm" />
                  </td>
                  <td data-slot="amount" className="py-2 text-right">
                    {/* Signed here, because a delta's sign *is* the number —
                        unlike a transaction amount, whose sign lives in
                        `direction`. */}
                    <Money value={row.change} signed className="text-sm" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    case "anomalies":
      return card.data.anomalies.length ? (
        <ul className="divide-y divide-border">
          {card.data.anomalies.map((row) => (
            <li key={row.id} className="py-2.5 text-sm">
              {/* Never called fraud, here or anywhere. Each carries the
                  figures the detector fired on. */}
              <p>{row.reason}</p>
              <p className="mt-0.5 text-xs text-muted">
                {formatDate(row.detected_on)}
                {row.category_name && ` · ${row.category_name}`}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">Nothing stands out.</p>
      );
  }
}
