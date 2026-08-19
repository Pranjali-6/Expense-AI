"use client";

import * as React from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Clock, Stethoscope } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { statements, type StatementHealth, type StatementSummary } from "@/lib/api";
import { formatDate } from "@/lib/format";

const CHECK_LABELS: Record<string, string> = {
  structural_validation: "File structure",
  document_classification: "Document type",
  bank_detection: "Bank and parser",
  text_layer: "Text layer",
  extraction: "Transaction extraction",
  reconciliation: "Financial reconciliation",
  duplicates: "Duplicate detection",
};

function StatusIcon({ status }: { status: string }) {
  if (status === "pass")
    return <CheckCircle2 className="size-4 text-success-text" aria-hidden="true" />;
  if (status === "warn")
    return <AlertTriangle className="size-4 text-warning-text" aria-hidden="true" />;
  if (status === "fail")
    return <AlertTriangle className="size-4 text-error-text" aria-hidden="true" />;
  return <Clock className="size-4 text-subtle" aria-hidden="true" />;
}

/**
 * Reconciliation delta, in rupees.
 *
 * Stored in paise as an exact integer so "is it zero?" is an integer
 * comparison rather than a float epsilon argument. A null delta means
 * reconciliation has not run — deliberately different from a zero delta, which
 * would claim a balanced statement nobody checked.
 */
function deltaLabel(paise: number | null): string {
  // A null delta means the statement did not print both balances, so nothing
  // could be checked. Rendering that as ₹0.00 would claim a balance nobody
  // verified — the exact difference the trust status is built on.
  if (paise === null) return "Could not be checked";
  if (paise === 0) return "₹0.00 — balances exactly";
  const rupees = (Math.abs(paise) / 100).toFixed(2);
  return `${paise < 0 ? "−" : "+"}₹${rupees} unaccounted`;
}

/** The one-line detail shown beside each check. */
function checkDetail(key: string, check: Record<string, unknown>): string {
  const status = String(check.status);
  if (status === "pending") return String(check.note ?? "pending");

  switch (key) {
    case "text_layer":
      return status === "warn"
        ? `${check.ocr_pages} of ${check.total_pages} pages needed OCR`
        : "pass";
    case "document_classification":
      return String(check.detected ?? "");
    case "bank_detection":
      return String(check.note ?? check.parser ?? "");
    case "extraction":
      return `${check.extracted} rows read${
        check.declared ? ` of ${check.declared} declared` : ""
      }`;
    case "reconciliation":
      if (check.note) return String(check.note);
      if (status === "pass") return "balances exactly";
      return check.first_divergent_row !== null &&
        check.first_divergent_row !== undefined
        ? `diverges at row ${check.first_divergent_row}`
        : "does not balance";
    case "duplicates": {
      const exact = Number(check.exact ?? 0);
      const suspected = Number(check.suspected ?? 0);
      if (!exact && !suspected) return "none found";
      const parts: string[] = [];
      if (exact) parts.push(`${exact} already imported`);
      if (suspected) parts.push(`${suspected} to review`);
      return parts.join(", ");
    }
    default:
      return status;
  }
}

function HealthCard({
  statement,
  health,
}: {
  statement: StatementSummary;
  health: StatementHealth | null;
}) {
  const ocrRatio =
    health && health.total_page_count > 0
      ? health.ocr_page_count / health.total_page_count
      : 0;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>
            {statement.bank_name ?? statement.bank_code ?? "Unidentified bank"}
            {statement.account_last4 && (
              <span className="ml-1.5 font-normal text-muted">
                ••••{statement.account_last4}
              </span>
            )}
          </CardTitle>
          <Badge variant={statement.trust_status === "trusted" ? "success" : "neutral"}>
            {statement.trust_status === "trusted"
              ? "Trusted"
              : statement.trust_status === "untrusted"
                ? "Does not reconcile"
                : "Not yet reconciled"}
          </Badge>
        </div>
        <p className="text-xs text-muted">
          Imported {formatDate(statement.created_at)}
          {statement.period_start &&
            ` · covers ${formatDate(statement.period_start)} – ${formatDate(statement.period_end)}`}
        </p>
      </CardHeader>

      <CardContent>
        {!health ? (
          <p className="text-sm text-muted">No health report yet.</p>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">Reconciliation</dt>
                <dd
                  data-slot="amount"
                  className={
                    health.reconciliation_delta_paise === 0
                      ? "mt-1 text-sm font-medium text-success-text"
                      : "mt-1 text-sm font-medium text-muted"
                  }
                >
                  {deltaLabel(health.reconciliation_delta_paise)}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">Transactions</dt>
                <dd data-slot="amount" className="mt-1 text-sm font-medium">
                  {health.extracted_transaction_count}
                  {health.declared_transaction_count !== null &&
                    ` of ${health.declared_transaction_count}`}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">Pages</dt>
                <dd data-slot="amount" className="mt-1 text-sm font-medium">
                  {health.total_page_count}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">Read by OCR</dt>
                <dd data-slot="amount" className="mt-1 text-sm font-medium">
                  {health.ocr_page_count}
                  {health.total_page_count > 0 && (
                    <span className="ml-1 text-xs font-normal text-muted">
                      ({Math.round(ocrRatio * 100)}%)
                    </span>
                  )}
                </dd>
              </div>
            </dl>

            {health.first_divergent_row !== null && (
              <p className="mt-4 flex items-start gap-2 rounded-md bg-error-subtle px-3 py-2 text-sm text-error-text">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                The running balance first stops following at row{" "}
                {health.first_divergent_row}
                {health.first_divergent_page !== null &&
                  ` on page ${health.first_divergent_page}`}
                . That is where extraction went wrong.
              </p>
            )}

            {health.checks && (
              <ul className="mt-4 divide-y divide-border border-t border-border">
                {Object.entries(health.checks).map(([key, check]) => (
                  <li key={key} className="flex items-center gap-3 py-2.5 text-sm">
                    <StatusIcon status={String(check.status)} />
                    <span className="flex-1">{CHECK_LABELS[key] ?? key}</span>
                    <span className="text-xs text-muted">
                      {checkDetail(key, check)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function StatementHealthPage() {
  const { data: list, isLoading } = useQuery({
    queryKey: ["statements"],
    queryFn: statements.list,
  });

  const healthQueries = useQueries({
    queries: (list ?? []).map((statement) => ({
      queryKey: ["statement-health", statement.id],
      queryFn: () => statements.health(statement.id).catch(() => null),
      enabled: Boolean(list?.length),
    })),
  });

  return (
    <>
      <PageHeader
        title="Statement health"
        description="Whether each import can be trusted — and if not, precisely where it broke."
      />

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-48 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      ) : !list?.length ? (
        <EmptyState
          icon={Stethoscope}
          title="No imports to assess"
          description="Every statement you upload gets a health report showing its reconciliation delta and extraction quality."
        />
      ) : (
        <div className="space-y-4">
          {list.map((statement, index) => (
            <HealthCard
              key={statement.id}
              statement={statement}
              health={(healthQueries[index]?.data as StatementHealth | null) ?? null}
            />
          ))}
        </div>
      )}
    </>
  );
}
