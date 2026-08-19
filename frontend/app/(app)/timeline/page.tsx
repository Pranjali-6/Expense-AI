"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRightLeft,
  CalendarClock,
  FileText,
  History,
  Receipt,
} from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Money } from "@/components/shared/money";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { intelligence, type TimelineItem } from "@/lib/api";
import { formatDate } from "@/lib/format";

const KIND: Record<
  string,
  { icon: typeof Receipt; tone: string; label: string }
> = {
  transaction: { icon: Receipt, tone: "text-muted", label: "Transaction" },
  large_transaction: { icon: ArrowRightLeft, tone: "text-info-text", label: "Large payment" },
  statement_import: { icon: FileText, tone: "text-primary-text", label: "Statement" },
  anomaly: { icon: AlertTriangle, tone: "text-warning-text", label: "Unusual" },
  subscription_renewal: { icon: CalendarClock, tone: "text-info-text", label: "Renewal due" },
};

function monthOf(iso: string): string {
  const [year, month] = iso.split("-");
  return new Date(Number(year), Number(month) - 1, 1).toLocaleDateString("en-IN", {
    month: "long",
    year: "numeric",
  });
}

export default function TimelinePage() {
  const [dense, setDense] = React.useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["timeline", dense],
    queryFn: () =>
      intelligence.timeline({ limit: 250, include_transactions: dense }),
  });

  // Grouped by month so the spine has anchors rather than being one long list.
  const grouped = React.useMemo(() => {
    const buckets = new Map<string, TimelineItem[]>();
    for (const item of data ?? []) {
      const key = item.occurred_on.slice(0, 7);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key)!.push(item);
    }
    return [...buckets.entries()];
  }, [data]);

  return (
    <>
      <PageHeader
        title="Financial timeline"
        description="Everything that happened, in order: imports, large payments, renewals and outliers."
        actions={
          <Button variant="secondary" onClick={() => setDense((value) => !value)}>
            {dense ? "Show highlights only" : "Show every transaction"}
          </Button>
        }
      />

      {isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : !data?.length ? (
        <EmptyState
          icon={History}
          title="Nothing on the timeline yet"
          description="Import a statement and its transactions, renewals and anything unusual appear here in order."
          action={
            <Button variant="primary" asChild>
              <Link href="/upload">Upload a statement</Link>
            </Button>
          }
        />
      ) : (
        <div className="space-y-8">
          {grouped.map(([month, items]) => (
            <section key={month}>
              <h2 className="sticky top-0 z-10 -mx-1 bg-background/90 px-1 py-2 text-sm font-semibold backdrop-blur">
                {monthOf(month)}
                <span className="ml-2 font-normal text-muted">{items.length} events</span>
              </h2>

              <ol className="relative ml-3 border-l border-border pl-6">
                {items.map((item, index) => {
                  const config = KIND[item.kind] ?? KIND.transaction!;
                  const Icon = config.icon;
                  return (
                    <li key={`${item.occurred_on}-${index}`} className="relative py-2.5">
                      <span
                        className="absolute -left-[31px] flex size-5 items-center justify-center rounded-full border border-border bg-surface"
                        aria-hidden="true"
                      >
                        <Icon className={`size-3 ${config.tone}`} />
                      </span>
                      <Card>
                        <CardContent className="flex flex-wrap items-start gap-3 p-3">
                          <div className="min-w-0 flex-1">
                            <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                              {item.title}
                              <Badge variant="neutral">{config.label}</Badge>
                            </p>
                            {item.summary && (
                              <p className="mt-0.5 text-xs text-muted">{item.summary}</p>
                            )}
                            <p className="mt-0.5 text-xs text-subtle">
                              {formatDate(item.occurred_on)}
                            </p>
                          </div>
                          {item.amount && (
                            <Money value={item.amount} className="shrink-0 text-sm" />
                          )}
                        </CardContent>
                      </Card>
                    </li>
                  );
                })}
              </ol>
            </section>
          ))}
        </div>
      )}
    </>
  );
}
