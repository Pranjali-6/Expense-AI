"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Info, Lock, ShieldCheck, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfidenceBars } from "@/components/shared/confidence-bars";
import { Money } from "@/components/shared/money";
import { transactions, type Transaction } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";

const CATEGORIES = [
  "food", "grocery", "rent", "utilities", "shopping", "travel", "fuel",
  "entertainment", "subscriptions", "healthcare", "insurance", "education",
  "emi", "investment", "salary", "bank_charges", "taxes", "cash_withdrawal",
  "transfers", "credit_card_payment", "refund", "other",
];

const SOURCE_LABEL: Record<string, string> = {
  user_rule: "Your rule",
  verified_merchant_rule: "Merchant dictionary",
  deterministic_rule: "Deterministic rule",
  historical_pattern: "Your history",
  ai_model: "AI suggestion",
  fallback_other: "Uncategorised",
};

export function TransactionPanel({
  transaction,
  onClose,
}: {
  transaction: Transaction | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const id = transaction?.id ?? null;

  const detail = useQuery({
    queryKey: ["transaction", id],
    queryFn: () => transactions.get(id!),
    enabled: Boolean(id),
  });

  const explanation = useQuery({
    queryKey: ["transaction-explain", id],
    queryFn: () => transactions.explain(id!),
    enabled: Boolean(id),
  });

  const history = useQuery({
    queryKey: ["transaction-audit", id],
    queryFn: () => transactions.audit(id!),
    enabled: Boolean(id),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    queryClient.invalidateQueries({ queryKey: ["transaction", id] });
    queryClient.invalidateQueries({ queryKey: ["transaction-explain", id] });
    queryClient.invalidateQueries({ queryKey: ["transaction-audit", id] });
    queryClient.invalidateQueries({ queryKey: ["review-stats"] });
  };

  const recategorise = useMutation({
    mutationFn: (category_slug: string) =>
      transactions.correct(id!, { category_slug, verify: true }),
    onSuccess: invalidate,
  });

  const approve = useMutation({
    mutationFn: () => transactions.bulkApprove([id!]),
    onSuccess: invalidate,
  });

  const applyToSimilar = useMutation({
    mutationFn: (category_slug: string) => transactions.applyToSimilar(id!, category_slug),
    onSuccess: invalidate,
  });

  const row = detail.data;
  const corrected =
    row &&
    (row.original_description !== row.description ||
      row.original_amount !== row.amount ||
      row.original_direction !== row.direction ||
      row.original_merchant !== row.merchant);

  return (
    <Sheet open={Boolean(transaction)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        <div className="border-b border-border px-6 py-4">
          <SheetTitle>{transaction?.merchant ?? "Transaction"}</SheetTitle>
        </div>

        {!row ? (
          <div className="space-y-4 p-6">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        ) : (
          <div className="space-y-6 p-6 pt-2">
            <div>
              <Money
                value={row.amount}
                direction={row.direction}
                emphasis="display"
                signed
              />
              <p className="mt-1 text-sm text-muted">
                {formatDate(row.txn_date)} · {row.bank_name ?? row.bank_code} ••••
                {row.account_last4}
              </p>
              <p className="mt-2 break-words text-xs text-muted">{row.description}</p>
            </div>

            <div className="flex flex-wrap gap-2">
              {row.category_name && <Badge variant="neutral">{row.category_name}</Badge>}
              {!row.is_expense && (
                <Badge variant="info">Not counted as spending</Badge>
              )}
              {row.transfer_group_id && <Badge variant="info">Internal movement</Badge>}
              {row.is_verified && (
                <Badge variant="success">
                  <ShieldCheck className="size-3" />
                  Verified by you
                </Badge>
              )}
            </div>

            {/* --- why this category ------------------------------------- */}
            <section>
              <h3 className="flex items-center gap-2 text-sm font-semibold">
                <Info className="size-4 text-primary-text" aria-hidden="true" />
                Why was this categorised this way?
              </h3>
              {explanation.isLoading ? (
                <Skeleton className="mt-2 h-12 w-full" />
              ) : explanation.data ? (
                <>
                  <p className="mt-2 rounded-md bg-surface-sunken px-3 py-2 text-sm">
                    {explanation.data.sentence}
                  </p>
                  <p className="mt-1.5 text-xs text-muted">
                    Source: {SOURCE_LABEL[explanation.data.source] ?? explanation.data.source}
                    {explanation.data.provenance.page !== null && (
                      <>
                        {" · "}read from page {explanation.data.provenance.page}, row{" "}
                        {explanation.data.provenance.row}
                      </>
                    )}
                  </p>
                </>
              ) : null}
            </section>

            <Separator />

            {/* --- confidence -------------------------------------------- */}
            <section>
              <h3 className="text-sm font-semibold">Confidence</h3>
              <p className="mb-3 mt-1 text-xs text-muted">
                Four independent scores. The gate is the lowest, never the average — a
                perfect category on a misread amount is still a misread amount.
              </p>
              <ConfidenceBars
                confidence={{
                  extraction: Number(row.confidence_extraction),
                  merchant: Number(row.confidence_merchant),
                  category: Number(row.confidence_category),
                  validation: Number(row.confidence_validation),
                }}
              />
            </section>

            <Separator />

            {/* --- recategorise ------------------------------------------ */}
            <section>
              <h3 className="text-sm font-semibold">Category</h3>
              <p className="mb-2 mt-1 text-xs text-muted">
                Your choice outranks every automatic rule, permanently.
              </p>
              <div className="flex flex-wrap gap-1.5">
                {CATEGORIES.map((slug) => (
                  <button
                    key={slug}
                    type="button"
                    disabled={recategorise.isPending}
                    onClick={() => recategorise.mutate(slug)}
                    className={
                      slug === row.category_slug
                        ? "rounded-md border border-primary bg-primary-subtle px-2 py-1 text-xs font-medium text-primary-text"
                        : "rounded-md border border-border px-2 py-1 text-xs text-muted transition-colors hover:border-primary hover:text-foreground"
                    }
                  >
                    {slug.replace(/_/g, " ")}
                  </button>
                ))}
              </div>

              {row.merchant && row.category_slug && (
                <Button
                  variant="secondary"
                  size="sm"
                  className="mt-3"
                  disabled={applyToSimilar.isPending}
                  onClick={() => applyToSimilar.mutate(row.category_slug!)}
                >
                  <Sparkles className="size-4" />
                  Apply to every other {row.merchant} transaction
                </Button>
              )}
              {applyToSimilar.data && (
                <p className="mt-2 text-xs text-success-text">
                  Updated {applyToSimilar.data.updated} other transactions. Rows you had
                  already verified were left alone.
                </p>
              )}
            </section>

            {row.review_status !== "resolved" && !row.is_verified && (
              <Button
                variant="primary"
                className="w-full"
                disabled={approve.isPending}
                onClick={() => approve.mutate()}
              >
                <ShieldCheck className="size-4" />
                Looks right — accept as read
              </Button>
            )}

            <Separator />

            {/* --- what the statement said ------------------------------- */}
            <section>
              <h3 className="flex items-center gap-2 text-sm font-semibold">
                <Lock className="size-4 text-muted" aria-hidden="true" />
                As printed on the statement
              </h3>
              <p className="mb-2 mt-1 text-xs text-muted">
                Never overwritten. Corrections are stored beside the original, so what
                the bank printed stays recoverable.
              </p>
              <dl className="space-y-1.5 text-xs">
                <Original label="Date" value={formatDate(row.original_txn_date)} />
                <Original label="Amount" value={row.original_amount} />
                <Original label="Direction" value={row.original_direction} />
                <Original label="Description" value={row.original_description} />
                {row.original_merchant && (
                  <Original label="Merchant" value={row.original_merchant} />
                )}
              </dl>
              {corrected && (
                <p className="mt-2 text-xs text-warning-text">
                  This transaction has been corrected. The values above are the originals.
                </p>
              )}
            </section>

            {history.data && history.data.length > 0 && (
              <>
                <Separator />
                <section>
                  <h3 className="flex items-center gap-2 text-sm font-semibold">
                    <History className="size-4 text-muted" aria-hidden="true" />
                    Change history
                  </h3>
                  <ul className="mt-2 space-y-2 text-xs">
                    {history.data.map((entry, index) => (
                      <li key={index} className="rounded-md bg-surface-sunken px-3 py-2">
                        <span className="font-medium">{entry.field_name}</span>{" "}
                        <span className="text-muted">
                          {entry.old_value ?? "—"} → {entry.new_value ?? "—"}
                        </span>
                        <span className="ml-1 text-muted">
                          · {entry.changed_by_name ?? entry.actor_kind} ·{" "}
                          {formatDateTime(entry.changed_at)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              </>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Original({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3">
      <dt className="w-24 shrink-0 text-muted">{label}</dt>
      <dd className="break-words">{value}</dd>
    </div>
  );
}
