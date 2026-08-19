"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  AlertTriangle,
  Bell,
  CheckCheck,
  FileCheck2,
  Repeat,
  ScanSearch,
  Wallet,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { notifications, type Notification } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

const ICONS: Record<string, LucideIcon> = {
  statement_processed: FileCheck2,
  statement_failed: AlertTriangle,
  reconciliation_failed: AlertTriangle,
  review_required: ScanSearch,
  budget_breach: Wallet,
  anomaly_detected: ScanSearch,
  subscription_renewal: Repeat,
};

/** Where a notification takes you. Every one of these is actionable or it
 *  should not have been created. */
function href(item: Notification): string {
  switch (item.kind) {
    case "statement_failed":
    case "reconciliation_failed":
      return "/statements/health";
    case "review_required":
      return "/review";
    case "budget_breach":
      return "/budgets";
    case "anomaly_detected":
      return "/insights";
    case "subscription_renewal":
      return "/subscriptions";
    default:
      return "/statements";
  }
}

export default function NotificationsPage() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["notifications"],
    queryFn: () => notifications.list(),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["notifications"] });

  const markRead = useMutation({ mutationFn: notifications.markRead, onSuccess: invalidate });
  const markAll = useMutation({ mutationFn: notifications.markAllRead, onSuccess: invalidate });

  const items = query.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="Notifications"
        description="Imports, reconciliation failures, budget breaches and statistical outliers."
        actions={
          query.data && query.data.unread > 0 ? (
            <Button
              variant="secondary"
              onClick={() => markAll.mutate()}
              disabled={markAll.isPending}
            >
              <CheckCheck className="size-4" />
              Mark all read
            </Button>
          ) : undefined
        }
      />

      {query.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : !items.length ? (
        <EmptyState
          icon={Bell}
          title="Nothing to report"
          description="You are notified when an import finishes, when a statement does not reconcile, when a budget is breached, and when something stands out statistically."
        />
      ) : (
        <Card>
          <ul className="divide-y divide-border">
            {items.map((item) => {
              const Icon = ICONS[item.kind] ?? Bell;
              const unread = item.read_at === null;
              return (
                <li
                  key={item.id}
                  className={
                    unread ? "flex gap-3 bg-primary-subtle/20 p-4" : "flex gap-3 p-4"
                  }
                >
                  <Icon
                    className={
                      unread
                        ? "mt-0.5 size-4 shrink-0 text-primary-text"
                        : "mt-0.5 size-4 shrink-0 text-muted"
                    }
                    aria-hidden="true"
                  />
                  <div className="min-w-0 flex-1">
                    <Link href={href(item)} className="text-sm font-medium hover:underline">
                      {item.title}
                    </Link>
                    {item.body && <p className="mt-0.5 text-sm text-muted">{item.body}</p>}
                    <p className="mt-1 text-xs text-subtle">
                      {formatDateTime(item.created_at)}
                    </p>
                  </div>
                  {unread && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => markRead.mutate(item.id)}
                      aria-label="Mark as read"
                    >
                      Mark read
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        </Card>
      )}
    </>
  );
}
