"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Stethoscope, Trash2, Upload } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { TableSkeleton } from "@/components/ui/skeleton";
import { statements, type StatementSummary } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";

function trustBadge(statement: StatementSummary) {
  // Never claim more than the pipeline verified. `pending` means reconciliation
  // has not run, which is not the same as "balanced".
  if (statement.status === "failed") return { variant: "error" as const, label: "Failed" };
  if (statement.trust_status === "trusted") return { variant: "success" as const, label: "Trusted" };
  if (statement.trust_status === "untrusted") return { variant: "error" as const, label: "Does not reconcile" };
  return { variant: "neutral" as const, label: "Not yet reconciled" };
}

function sizeLabel(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(0)} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function StatementsPage() {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["statements"],
    queryFn: statements.list,
    // Statements move through the pipeline in the background, so the list
    // refreshes while anything is still processing.
    refetchInterval: (query) =>
      query.state.data?.some((s) => s.status === "uploaded" || s.status === "processing")
        ? 3000
        : false,
  });

  const remove = useMutation({
    mutationFn: statements.remove,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["statements"] }),
  });

  return (
    <>
      <PageHeader
        title="Statements"
        description="Every statement you have imported, and whether it can be trusted."
        actions={
          <>
            <Button variant="secondary" asChild>
              <Link href="/statements/health">
                <Stethoscope className="size-4" />
                Statement health
              </Link>
            </Button>
            <Button variant="primary" asChild>
              <Link href="/upload">
                <Upload className="size-4" />
                Upload
              </Link>
            </Button>
          </>
        }
      />

      {isLoading ? (
        <Card>
          <TableSkeleton rows={5} />
        </Card>
      ) : !data?.length ? (
        <EmptyState
          icon={FileText}
          title="No statements imported"
          description="Upload a PDF and it appears here with its bank, period, transaction count and validation status."
          action={
            <Button variant="primary" asChild>
              <Link href="/upload">Upload a statement</Link>
            </Button>
          }
        />
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                <tr>
                  <th scope="col" className="px-4 py-3 font-medium">Statement</th>
                  <th scope="col" className="px-4 py-3 font-medium">Type</th>
                  <th scope="col" className="px-4 py-3 font-medium">Period</th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">Pages</th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">Read</th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">In ledger</th>
                  <th scope="col" className="px-4 py-3 font-medium">Status</th>
                  <th scope="col" className="px-4 py-3"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.map((statement) => {
                  const trust = trustBadge(statement);
                  const busy =
                    statement.status === "uploaded" || statement.status === "processing";

                  return (
                    <tr key={statement.id} className="hover:bg-surface-sunken/50">
                      <td className="px-4 py-3">
                        <p className="font-medium">
                          {statement.bank_name ?? statement.bank_code ?? "Unidentified bank"}
                          {statement.account_last4 && (
                            <span className="ml-1.5 text-muted">••••{statement.account_last4}</span>
                          )}
                        </p>
                        <p className="text-xs text-muted">
                          {formatDateTime(statement.created_at)} · {sizeLabel(statement.file_size_bytes)}
                        </p>
                      </td>
                      <td className="px-4 py-3 text-muted">
                        {statement.document_type === "credit_card_statement"
                          ? "Credit card"
                          : statement.document_type === "bank_statement"
                            ? "Bank"
                            : "Unknown"}
                      </td>
                      <td className="px-4 py-3 text-muted">
                        {statement.period_start
                          ? `${formatDate(statement.period_start)} – ${formatDate(statement.period_end)}`
                          : "—"}
                      </td>
                      <td data-slot="amount" className="px-4 py-3 text-right text-muted">
                        {statement.page_count ?? "—"}
                      </td>
                      <td data-slot="amount" className="px-4 py-3 text-right text-muted">
                        {statement.extracted_transaction_count || "—"}
                      </td>
                      <td data-slot="amount" className="px-4 py-3 text-right">
                        {statement.transaction_count}
                      </td>
                      <td className="px-4 py-3">
                        {busy ? (
                          <Badge variant="info">
                            Processing {statement.progress ?? 0}%
                          </Badge>
                        ) : (
                          <Badge variant={trust.variant}>{trust.label}</Badge>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label="Delete this statement"
                          disabled={remove.isPending}
                          onClick={() => {
                            if (
                              confirm(
                                "Delete this statement and every transaction it produced?",
                              )
                            ) {
                              remove.mutate(statement.id);
                            }
                          }}
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
