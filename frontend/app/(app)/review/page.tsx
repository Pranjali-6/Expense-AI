"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ClipboardCheck } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { StatCard } from "@/components/shared/stat-card";
import { Money } from "@/components/shared/money";
import { ConfidenceChip } from "@/components/shared/confidence-bars";
import { TransactionPanel } from "@/components/transactions/transaction-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { TableSkeleton } from "@/components/ui/skeleton";
import { transactions, type Transaction } from "@/lib/api";
import { formatDate } from "@/lib/format";

const WEAKEST_EXPLANATION: Record<string, string> = {
  extraction: "the row may have been misread off the statement",
  merchant: "we could not identify who this was paid to",
  category: "no rule matched, so the category is a guess",
  validation: "the statement this came from does not reconcile",
};

export default function ReviewPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = React.useState<Transaction | null>(null);
  const [checked, setChecked] = React.useState<Set<string>>(new Set());

  const stats = useQuery({ queryKey: ["review-stats"], queryFn: transactions.reviewStats });

  const queue = useQuery({
    queryKey: ["transactions", { review_status: "review_required" }, 0],
    queryFn: () =>
      transactions.list({ review_status: "review_required", limit: 100 }),
  });

  const flagged = useQuery({
    queryKey: ["transactions", { review_status: "flagged" }, 0],
    queryFn: () => transactions.list({ review_status: "flagged", limit: 100 }),
  });

  const approve = useMutation({
    mutationFn: (ids: string[]) => transactions.bulkApprove(ids),
    onSuccess: () => {
      setChecked(new Set());
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["review-stats"] });
    },
  });

  const rows = [...(queue.data?.items ?? []), ...(flagged.data?.items ?? [])];
  const loading = queue.isLoading || flagged.isLoading;

  const toggle = (id: string) =>
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <>
      <PageHeader
        title="Review center"
        description="Only what the pipeline was not sure about. Everything else is already in the ledger."
        actions={
          checked.size > 0 ? (
            <Button
              variant="primary"
              disabled={approve.isPending}
              onClick={() => approve.mutate([...checked])}
            >
              <CheckCircle2 className="size-4" />
              Accept {checked.size} as read
            </Button>
          ) : undefined
        }
      />

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          money={false}
          label="Needs review"
          value={stats.data?.review_required ?? 0}
          hint="Confidence below 90%"
        />
        <StatCard
          money={false}
          label="Flagged"
          value={stats.data?.flagged ?? 0}
          hint="90–97% — counted in totals, but marked"
        />
        <StatCard
          money={false}
          label="Auto-approved"
          value={stats.data?.auto_approved ?? 0}
          hint="97% or above on all four dimensions"
        />
        <StatCard
          money={false}
          label="Uncategorised"
          value={stats.data?.uncategorised ?? 0}
          hint="No rule or merchant matched"
        />
      </div>

      {loading ? (
        <Card>
          <TableSkeleton rows={5} />
        </Card>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={ClipboardCheck}
          title="Nothing needs your attention"
          description="Every imported transaction cleared the confidence gate on all four dimensions. Statements that do not reconcile would appear here."
          action={
            <Button variant="secondary" asChild>
              <Link href="/transactions">Open the ledger</Link>
            </Button>
          }
        />
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th scope="col" className="w-10 px-4 py-3">
                    <span className="sr-only">Select</span>
                  </th>
                  <th scope="col" className="px-4 py-3 font-medium">Date</th>
                  <th scope="col" className="px-4 py-3 font-medium">Transaction</th>
                  <th scope="col" className="px-4 py-3 font-medium">Why it is here</th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">Amount</th>
                  <th scope="col" className="px-4 py-3 font-medium">Confidence</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((row) => {
                  // Derived from the four scores rather than read from the
                  // stored reason: the list endpoint returns the scores, and
                  // computing from them cannot disagree with what the chip
                  // beside it shows.
                  const dimension = weakestOf(row);

                  return (
                    <tr key={row.id} className="hover:bg-surface-sunken/60">
                      <td className="px-4 py-3">
                        <input
                          type="checkbox"
                          className="size-4 accent-[var(--color-primary)]"
                          checked={checked.has(row.id)}
                          onChange={() => toggle(row.id)}
                          aria-label={`Select transaction from ${formatDate(row.txn_date)}`}
                        />
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-muted">
                        {formatDate(row.txn_date)}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          onClick={() => setSelected(row)}
                          className="text-left font-medium hover:underline"
                        >
                          {row.merchant ?? "No merchant identified"}
                        </button>
                        <p className="max-w-[32ch] truncate text-xs text-muted">
                          {row.description}
                        </p>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted">
                        {WEAKEST_EXPLANATION[dimension] ?? "confidence below the gate"}
                        {row.category_name && (
                          <Badge variant="neutral" className="ml-2">
                            {row.category_name}
                          </Badge>
                        )}
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
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <TransactionPanel transaction={selected} onClose={() => setSelected(null)} />
    </>
  );
}

/** Which of the four dimensions is dragging this row below the gate. */
function weakestOf(row: Transaction): string {
  const scores: [string, number][] = [
    ["extraction", Number(row.confidence_extraction)],
    ["merchant", Number(row.confidence_merchant)],
    ["category", Number(row.confidence_category)],
    ["validation", Number(row.confidence_validation)],
  ];
  return scores.reduce((low, current) => (current[1] < low[1] ? current : low))[0];
}
