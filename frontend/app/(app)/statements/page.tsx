"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Loader2, Lock, Stethoscope, Trash2, Upload } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { TableSkeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { ApiError, statements, type StatementSummary } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";

function trustBadge(statement: StatementSummary) {
  // Never claim more than the pipeline verified. `pending` means reconciliation
  // has not run, which is not the same as "balanced".
  if (statement.status === "failed") return { variant: "error" as const, label: "Failed" };
  // Waiting on the user, not broken. Rendering this as a failure would tell
  // someone their statement is unreadable when it is one password away.
  if (statement.status === "password_required")
    return { variant: "warning" as const, label: "Locked" };
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

  const locked = data?.filter((s) => s.status === "password_required") ?? [];

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

      {locked.length > 0 && (
        <section aria-label="Statements needing a password" className="mb-4 space-y-3">
          {locked.map((statement) => (
            <UnlockCard key={statement.id} statement={statement} />
          ))}
        </section>
      )}

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


/** A password prompt for one stored-but-locked statement.
 *
 *  Lives on this page as well as on Upload because the upload view is
 *  transient: a user who navigates away mid-import would otherwise have no
 *  route back to a file the server is already holding. */
function UnlockCard({ statement }: { statement: StatementSummary }) {
  const queryClient = useQueryClient();
  const [password, setPassword] = React.useState("");
  const [message, setMessage] = React.useState<string | null>(null);
  const [remaining, setRemaining] = React.useState<number | null>(null);

  const unlock = useMutation({
    mutationFn: () => statements.unlock(statement.id, password),
    onSuccess: (response) => {
      setPassword("");
      if (response.unlocked) {
        setMessage(null);
        queryClient.invalidateQueries({ queryKey: ["statements"] });
        return;
      }
      setMessage(response.message ?? "That password did not open the statement.");
      setRemaining(response.attempts_remaining);
    },
    onError: (err) => {
      setPassword("");
      setMessage(
        err instanceof ApiError ? err.message : "Could not unlock the statement.",
      );
    },
  });

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start gap-3">
          <Lock className="mt-0.5 size-4 shrink-0 text-warning-text" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">
              {statement.bank_name ?? statement.bank_code ?? "A statement"} needs
              a password
            </p>
            <p className="mt-0.5 text-sm text-muted">
              Uploaded {formatDateTime(statement.created_at)} &middot;{" "}
              {sizeLabel(statement.file_size_bytes)}. The file is saved and
              encrypted &mdash; it just cannot be read yet.
            </p>
          </div>
          <Badge variant="warning">Locked</Badge>
        </div>

        <form
          className="mt-3 flex flex-wrap items-start gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (password) unlock.mutate();
          }}
        >
          <label className="sr-only" htmlFor={`unlock-${statement.id}`}>
            Statement password
          </label>
          <Input
            id={`unlock-${statement.id}`}
            type="password"
            autoComplete="off"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Statement password"
            className="min-w-0 flex-1 sm:max-w-xs"
            disabled={unlock.isPending}
          />
          <Button
            type="submit"
            variant="primary"
            disabled={unlock.isPending || !password}
          >
            {unlock.isPending && <Loader2 className="size-4 animate-spin" />}
            {unlock.isPending ? "Opening…" : "Unlock"}
          </Button>
        </form>

        {message && (
          <p role="alert" className="mt-2 text-sm text-error-text">
            {message}
            {remaining !== null && remaining > 0 && (
              <span className="text-muted">
                {" "}
                {remaining} attempt{remaining === 1 ? "" : "s"} left.
              </span>
            )}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
