"use client";

import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowRightLeft, Download, Receipt, Search, X } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Money } from "@/components/shared/money";
import { ConfidenceChip } from "@/components/shared/confidence-bars";
import { TransactionPanel } from "@/components/transactions/transaction-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { TableSkeleton } from "@/components/ui/skeleton";
import {
  exports,
  transactions,
  type ExportFormat,
  type Transaction,
  type TransactionFilters,
} from "@/lib/api";
import { formatDate } from "@/lib/format";

const PAGE_SIZE = 50;

const CATEGORY_OPTIONS = [
  "", "food", "grocery", "rent", "utilities", "shopping", "travel", "fuel",
  "entertainment", "subscriptions", "healthcare", "insurance", "education",
  "emi", "investment", "salary", "bank_charges", "taxes", "cash_withdrawal",
  "transfers", "credit_card_payment", "refund", "other",
];

export default function TransactionsPage() {
  return (
    // useSearchParams suspends during prerender; without a boundary the whole
    // route falls back to client-side rendering with a build warning.
    <React.Suspense fallback={<TableSkeleton rows={8} />}>
      <TransactionsView />
    </React.Suspense>
  );
}

function TransactionsView() {
  const params = useSearchParams();

  // Seeded from the URL so links elsewhere in the app actually land somewhere.
  // The Accounts screen links here with `?account_id=`, and the assistant's
  // "Open in Transactions" reproduces a whole answer as real filters — a link
  // that dropped half of them would show a different set of rows than the
  // answer was computed from, which is worse than no link at all.
  const [filters, setFilters] = React.useState<TransactionFilters>(() => {
    const initial: TransactionFilters = {};
    const carried: (keyof TransactionFilters)[] = [
      "account_id", "category", "review_status", "merchant", "search",
      "date_from", "date_to", "min_amount", "max_amount", "direction",
    ];
    for (const key of carried) {
      const value = params.get(key);
      if (value) (initial as Record<string, string>)[key] = value;
    }
    return initial;
  });
  const [search, setSearch] = React.useState("");
  const [page, setPage] = React.useState(0);
  const [selected, setSelected] = React.useState<Transaction | null>(null);
  const [exportFormat, setExportFormat] = React.useState<ExportFormat>("csv");

  // Debounced so typing does not fire a request per keystroke.
  React.useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((current) => ({ ...current, search: search || undefined }));
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const query = useQuery({
    queryKey: ["transactions", filters, page],
    queryFn: () =>
      transactions.list({ ...filters, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
  });

  const download = useMutation({
    mutationFn: () => {
      // `limit` and `offset` are paging, not filtering — sending them would
      // export one screenful and call it the ledger.
      const { limit: _limit, offset: _offset, ...criteria } = filters;
      return exports.transactions(exportFormat, criteria);
    },
  });

  // `?id=` opens a specific transaction, so a row is linkable from anywhere.
  const deepLinked = params.get("id");
  React.useEffect(() => {
    if (!deepLinked || selected) return;
    const match = query.data?.items.find((row) => row.id === deepLinked);
    if (match) setSelected(match);
  }, [deepLinked, query.data, selected]);

  const set = (patch: Partial<TransactionFilters>) => {
    setFilters((current) => ({ ...current, ...patch }));
    setPage(0);
  };

  const active = Object.entries(filters).filter(
    ([, value]) => value !== undefined && value !== "",
  );

  // Filters the control row cannot display. Labelled in words rather than by
  // key, because "date_from: 2025-03-01" is a debug string, not a filter chip.
  const CHIP_LABELS: Record<string, (value: string) => string> = {
    date_from: (value) => `From ${formatDate(value)}`,
    date_to: (value) => `To ${formatDate(value)}`,
    min_amount: (value) => `Above ₹${Number(value).toLocaleString("en-IN")}`,
    max_amount: (value) => `Below ₹${Number(value).toLocaleString("en-IN")}`,
    merchant: (value) => `Merchant: ${value}`,
    account_id: () => "One account",
  };

  const unlabelled = active.flatMap(([key, value]) => {
    const label = CHIP_LABELS[key];
    return label ? [[key, label(String(value))] as const] : [];
  });

  const total = query.data?.total ?? 0;
  const pages = Math.ceil(total / PAGE_SIZE);

  return (
    <>
      <PageHeader
        title="Transactions"
        description="Every transaction read from your statements. You never typed one in."
        actions={
          <>
            {/* Exports what is on screen, not the whole ledger: the filters
                above are the query, so the file and the table agree. */}
            <select
              aria-label="Export format"
              className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
              value={exportFormat}
              onChange={(event) =>
                setExportFormat(event.target.value as ExportFormat)
              }
            >
              <option value="csv">CSV</option>
              <option value="json">JSON</option>
              <option value="pdf">PDF</option>
            </select>
            <Button
              variant="secondary"
              disabled={download.isPending}
              onClick={() => download.mutate()}
            >
              <Download className="size-4" />
              Export
            </Button>
            <Button variant="secondary" asChild>
              <Link href="/review">Review center</Link>
            </Button>
          </>
        }
      />

      {/* Filters sit in one row above the table so the result set is always
          explainable from what is on screen. */}
      <Card className="mb-4 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[200px] flex-1">
            <Search
              className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted"
              aria-hidden="true"
            />
            <Input
              className="pl-8"
              placeholder="Search merchant or description"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              aria-label="Search transactions"
            />
          </div>

          <select
            aria-label="Category"
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
            value={filters.category ?? ""}
            onChange={(event) => set({ category: event.target.value || undefined })}
          >
            {CATEGORY_OPTIONS.map((slug) => (
              <option key={slug} value={slug}>
                {slug ? slug.replace(/_/g, " ") : "All categories"}
              </option>
            ))}
          </select>

          <select
            aria-label="Direction"
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
            value={filters.direction ?? ""}
            onChange={(event) => set({ direction: event.target.value || undefined })}
          >
            <option value="">In and out</option>
            <option value="debit">Money out</option>
            <option value="credit">Money in</option>
          </select>

          <select
            aria-label="Status"
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
            value={filters.review_status ?? ""}
            onChange={(event) => set({ review_status: event.target.value || undefined })}
          >
            <option value="">Any status</option>
            <option value="auto_approved">Auto-approved</option>
            <option value="flagged">Flagged</option>
            <option value="review_required">Needs review</option>
            <option value="resolved">Resolved</option>
          </select>

          <select
            aria-label="Counted as spending"
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
            value={filters.is_expense === undefined ? "" : String(filters.is_expense)}
            onChange={(event) =>
              set({
                is_expense:
                  event.target.value === "" ? undefined : event.target.value === "true",
              })
            }
          >
            <option value="">Everything</option>
            <option value="true">Spending only</option>
            <option value="false">Transfers &amp; income</option>
          </select>

          {active.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setFilters({});
                setSearch("");
                setPage(0);
              }}
            >
              <X className="size-4" />
              Clear
            </Button>
          )}
        </div>

        {/* Filters arriving by URL that have no control of their own — a date
            range or an amount floor from an assistant answer — are shown as
            removable chips. Without them the row above stops explaining the
            result set: the table would be filtered by something invisible,
            which is exactly the confusion the one-row layout exists to avoid. */}
        {unlabelled.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border pt-2">
            {unlabelled.map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => set({ [key]: undefined })}
                className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-sunken px-2.5 py-1 text-xs text-muted hover:text-foreground"
                aria-label={`Remove filter: ${label}`}
              >
                {label}
                <X className="size-3" aria-hidden="true" />
              </button>
            ))}
          </div>
        )}
      </Card>

      {query.isLoading ? (
        <Card>
          <TableSkeleton rows={8} />
        </Card>
      ) : !query.data?.items.length ? (
        <EmptyState
          icon={Receipt}
          title={active.length ? "Nothing matches those filters" : "No transactions yet"}
          description={
            active.length
              ? "Try widening the date range or clearing a filter."
              : "Upload a statement and every transaction in it appears here, categorised."
          }
          action={
            !active.length ? (
              <Button variant="primary" asChild>
                <Link href="/upload">Upload a statement</Link>
              </Button>
            ) : undefined
          }
        />
      ) : (
        <>
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th scope="col" className="px-4 py-3 font-medium">Date</th>
                    <th scope="col" className="px-4 py-3 font-medium">Merchant</th>
                    <th scope="col" className="px-4 py-3 font-medium">Category</th>
                    <th scope="col" className="px-4 py-3 font-medium">Account</th>
                    <th scope="col" className="px-4 py-3 text-right font-medium">Amount</th>
                    <th scope="col" className="px-4 py-3 font-medium">Confidence</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {query.data.items.map((row) => (
                    <tr
                      key={row.id}
                      tabIndex={0}
                      role="button"
                      onClick={() => setSelected(row)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelected(row);
                        }
                      }}
                      className="cursor-pointer hover:bg-surface-sunken/60 focus:bg-surface-sunken focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                    >
                      <td className="whitespace-nowrap px-4 py-3 text-muted">
                        {formatDate(row.txn_date)}
                      </td>
                      <td className="px-4 py-3">
                        <p className="font-medium">
                          {row.merchant ?? (
                            <span className="text-muted">No merchant</span>
                          )}
                        </p>
                        <p className="max-w-[28ch] truncate text-xs text-muted">
                          {row.description}
                        </p>
                      </td>
                      <td className="px-4 py-3">
                        {row.category_name ? (
                          <Badge variant="neutral">{row.category_name}</Badge>
                        ) : (
                          <span className="text-xs text-muted">—</span>
                        )}
                        {!row.is_expense && (
                          <span
                            className="ml-1.5 inline-flex align-middle text-info-text"
                            title="Not counted as spending"
                          >
                            <ArrowRightLeft className="size-3.5" aria-hidden="true" />
                          </span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs text-muted">
                        {row.bank_code} ••••{row.account_last4}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Money value={row.amount} direction={row.direction} signed />
                      </td>
                      <td className="px-4 py-3">
                        <ConfidenceChip
                          confidence={{
                            extraction: Number(row.confidence_extraction),
                            merchant: Number(row.confidence_merchant),
                            category: Number(row.confidence_category),
                            validation: Number(row.confidence_validation),
                          }}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="mt-4 flex items-center justify-between text-sm text-muted">
            <p>
              Showing {page * PAGE_SIZE + 1}–
              {Math.min((page + 1) * PAGE_SIZE, total)} of {total}
            </p>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={page === 0}
                onClick={() => setPage((current) => current - 1)}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={page + 1 >= pages}
                onClick={() => setPage((current) => current + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}

      <TransactionPanel transaction={selected} onClose={() => setSelected(null)} />
    </>
  );
}
