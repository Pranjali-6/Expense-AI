"use client";

import * as React from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Lightbulb, Sparkles } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { StatCard } from "@/components/shared/stat-card";
import { Money } from "@/components/shared/money";
import { CategoryBars } from "@/components/charts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { intelligence } from "@/lib/api";
import { formatDate } from "@/lib/format";

function monthName(iso: string): string {
  const [year, month] = iso.split("-");
  return new Date(Number(year), Number(month) - 1, 1).toLocaleDateString("en-IN", {
    month: "long",
    year: "numeric",
  });
}

export default function InsightsPage() {
  // Anchored to whatever month actually has data, because statements arrive
  // after the period they cover.
  const summary = useQuery({
    queryKey: ["intel-summary"],
    queryFn: () => intelligence.summary(),
  });
  const month = summary.data?.month.slice(0, 7);

  const insight = useQuery({
    queryKey: ["intel-insight", month],
    queryFn: () => intelligence.insights(month!),
    enabled: Boolean(month),
  });
  const categories = useQuery({
    queryKey: ["intel-categories", month],
    queryFn: () => intelligence.categories(month),
    enabled: Boolean(month),
  });

  const data = insight.data;

  return (
    <>
      <PageHeader
        title="Insights"
        description={
          data
            ? `${monthName(data.month)} — assembled from your ledger, not written by a model.`
            : "Your month, summarised."
        }
      />

      {insight.isLoading || !data ? (
        <div className="space-y-4">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : data.summary.transaction_count === 0 ? (
        <EmptyState
          icon={Lightbulb}
          title="Nothing to report yet"
          description="Import a statement and a monthly report is built from it automatically."
          action={
            <Button variant="primary" asChild>
              <Link href="/upload">Upload a statement</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="Spending" value={data.summary.net_expenses} />
            <StatCard label="Income" value={data.summary.income} />
            <StatCard label="Net" value={data.summary.net_cash_flow} />
            <StatCard
              label="Savings rate"
              value={`${Math.round(Number(data.summary.savings_rate) * 100)}%`}
              money={false}
            />
          </div>

          {/* The narrative, when one exists. Written from the stored snapshot
              — the same row the cards below are drawn from — so the paragraph
              and the figures cannot disagree. Absent whenever AI is off, which
              is the default; the observations underneath are the report either
              way, not a fallback for it. */}
          {data.narrative && (
            <Card className="mb-6 border-primary/30">
              <CardContent className="p-5">
                <p className="text-sm leading-relaxed">{data.narrative.text}</p>
                <p className="mt-3 text-xs text-muted">
                  Phrased by {data.narrative.model_name} from this month&rsquo;s
                  stored figures. Every number in it appears in the report below.
                </p>
              </CardContent>
            </Card>
          )}

          {/* Observations render as cards. The narrative above phrases exactly
              these, from exactly these numbers. */}
          <Card className="mb-6">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sparkles className="size-4 text-primary-text" aria-hidden="true" />
                What stood out
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="space-y-2.5">
                {data.observations.map((note) => (
                  <li key={note.kind} className="flex items-start gap-2.5 text-sm">
                    <span
                      className="mt-1.5 size-1.5 shrink-0 rounded-full bg-primary"
                      aria-hidden="true"
                    />
                    <span>{note.text}</span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>

          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Where it went</CardTitle>
              </CardHeader>
              <CardContent>
                {categories.data?.length ? (
                  <CategoryBars data={categories.data} limit={8} />
                ) : (
                  <p className="text-sm text-muted">Nothing categorised.</p>
                )}
              </CardContent>
            </Card>

            <div className="space-y-6">
              {data.largest_transaction && (
                <Card>
                  <CardHeader>
                    <CardTitle>Largest single payment</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <Money value={data.largest_transaction.amount} emphasis="display" />
                    <p className="mt-1 text-sm text-muted">
                      {data.largest_transaction.merchant ?? "Unnamed payee"}
                      {data.largest_transaction.category_name &&
                        ` · ${data.largest_transaction.category_name}`}
                      {" · "}
                      {formatDate(data.largest_transaction.txn_date)}
                    </p>
                  </CardContent>
                </Card>
              )}

              {data.recurring_load && data.recurring_load.count > 0 && (
                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      Recurring load
                      <Badge variant="neutral">{data.recurring_load.count} charges</Badge>
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <Money value={data.recurring_load.monthly_equivalent} emphasis="display" />
                    <p className="mt-1 text-sm text-muted">
                      a month, or <Money value={data.recurring_load.annual} className="text-sm" />{" "}
                      a year.{" "}
                      <Link href="/subscriptions" className="underline">
                        See them
                      </Link>
                      .
                    </p>
                  </CardContent>
                </Card>
              )}

              <Card>
                <CardHeader>
                  <CardTitle>Top merchants</CardTitle>
                </CardHeader>
                <CardContent>
                  <ul className="divide-y divide-border">
                    {data.top_merchants.map((row) => (
                      <li key={row.merchant} className="flex items-center gap-3 py-2 text-sm">
                        <span className="flex-1 truncate">{row.merchant}</span>
                        <span className="text-xs text-muted">
                          {row.transaction_count}×
                        </span>
                        <Money value={row.total} className="text-sm" />
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>
            </div>
          </div>
        </>
      )}
    </>
  );
}
