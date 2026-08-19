"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { TableSkeleton } from "@/components/ui/skeleton";
import { auditLog, type AuditEntryRow } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

const LABELS: Record<string, string> = {
  login: "Signed in",
  login_failed: "Sign-in failed",
  logout: "Signed out",
  register: "Account created",
  password_change: "Password changed",
  statement_upload: "Statement uploaded",
  statement_delete: "Statement deleted",
  statement_reprocess: "Statement reprocessed",
  transaction_edit: "Transaction corrected",
  transaction_approve: "Transaction approved",
  rule_create: "Rule created",
  rule_delete: "Rule deleted",
  account_delete: "Account removed",
  budget_change: "Budget changed",
  export: "Data exported",
  ai_toggle: "AI setting changed",
  data_delete: "Data deleted",
};

/** `details` is restricted to non-sensitive context when it is written, so it
 *  can be rendered as-is. Values are ids, counts and field *names*. */
function detailText(entry: AuditEntryRow): string {
  if (!entry.details || Object.keys(entry.details).length === 0) return "";
  return Object.entries(entry.details)
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${String(value)}`)
    .join(" · ");
}

export default function AuditPage() {
  const [action, setAction] = React.useState("");
  const [cursors, setCursors] = React.useState<string[]>([]);
  const before = cursors[cursors.length - 1];

  const query = useQuery({
    queryKey: ["audit", action, before],
    queryFn: () => auditLog.list({ action: action || undefined, before, limit: 50 }),
  });

  const items = query.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Audit log"
        description="Every action taken on this account. Append-only — nothing here can be edited, by you or by us."
      />

      <Card className="mb-4 p-3">
        <label className="text-xs text-muted">
          Action
          <select
            className="ml-2 h-9 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
            value={action}
            onChange={(event) => {
              setAction(event.target.value);
              setCursors([]);
            }}
          >
            <option value="">Everything</option>
            {Object.entries(LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </Card>

      {query.isLoading ? (
        <Card>
          <TableSkeleton rows={8} />
        </Card>
      ) : !items.length ? (
        <EmptyState
          icon={ScrollText}
          title="Nothing recorded yet"
          description="Signing in, uploading a statement, correcting a transaction and exporting data all appear here."
        />
      ) : (
        <>
          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th scope="col" className="px-4 py-3 font-medium">When</th>
                    <th scope="col" className="px-4 py-3 font-medium">Action</th>
                    <th scope="col" className="px-4 py-3 font-medium">Detail</th>
                    <th scope="col" className="px-4 py-3 font-medium">From</th>
                    <th scope="col" className="px-4 py-3 font-medium">Result</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {items.map((entry) => (
                    <tr key={entry.id} className="hover:bg-surface-sunken/50">
                      <td className="whitespace-nowrap px-4 py-3 text-muted">
                        {formatDateTime(entry.occurred_at)}
                      </td>
                      <td className="px-4 py-3 font-medium">
                        {LABELS[entry.action] ?? entry.action}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted">
                        {detailText(entry)}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted">
                        {entry.ip_address ?? "—"}
                      </td>
                      <td className="px-4 py-3">
                        <Badge variant={entry.succeeded ? "success" : "error"}>
                          {entry.succeeded ? "Succeeded" : "Failed"}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="mt-4 flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={!cursors.length}
              onClick={() => setCursors((current) => current.slice(0, -1))}
            >
              Newer
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!query.data?.next_before}
              onClick={() =>
                setCursors((current) => [...current, query.data!.next_before!])
              }
            >
              Older
            </Button>
          </div>
        </>
      )}
    </>
  );
}
