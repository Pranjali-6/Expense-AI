"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Banknote, PiggyBank, Receipt, TrendingUp } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { QueryBox } from "@/components/assistant/query-box";
import { EmptyState } from "@/components/shared/empty-state";
import { StatCard } from "@/components/shared/stat-card";
import { Money } from "@/components/shared/money";
import { CategoryBars, DailyBars, TrendChart } from "@/components/charts";
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

export default function DashboardPage() {
  const summary = useQuery({ queryKey: ["intel-summary"], queryFn: () => intelligence.summary() });
  const trend = useQuery({ queryKey: ["intel-trend"], queryFn: () => intelligence.trend(12) });
  const categories = useQuery({
    queryKey: ["intel-categories"],
    queryFn: () => intelligence.categories(),
  });
  const daily = useQuery({ queryKey: ["intel-daily"], queryFn: () => intelligence.daily() });
  const merchants = useQuery({
    queryKey: ["intel-merchants"],
    queryFn: () => intelligence.topMerchants(undefined, 5),
  });
  const anomalies = useQuery({
    queryKey: ["intel-anomalies"],
    queryFn: () => intelligence.anomalies(4),
  });

  const data = summary.data;
  const loading = summary.isLoading;
  const empty = !loading && data && data.transaction_count === 0;

  return (
    <>
      <PageHeader
        title="Dashboard"
        description={
          data
            ? `${monthName(data.month)} — every figure computed locally, no model involved.`
            : "Your money, summarised."
        }
        actions={
          <Button variant="secondary" asChild>
            <Link href="/insights">
              Monthly report
              <ArrowRight className="size-4" />
            </Link>
          </Button>
        }
      />

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((n) => (
            <Skeleton key={n} className="h-32 w-full" />
          ))}
        </div>
      ) : empty ? (
        <EmptyState
          icon={Receipt}
          title="No transactions yet"
          description="Upload a bank or credit-card statement and this fills in automatically. You will never type a transaction."
          action={
            <Button variant="primary" asChild>
              <Link href="/upload">Upload a statement</Link>
            </Button>
          }
        />
      ) : (
        data && (
          <>
            {/* Said up front, not buried: which of these numbers rest on
                statements nobody has verified. */}
            {!data.data_quality.fully_trusted && (
              <Card className="mb-4 border-warning">
                <CardContent className="flex items-start gap-3 p-4 text-sm">
                  <AlertTriangle
                    className="mt-0.5 size-4 shrink-0 text-warning-text"
                    aria-hidden="true"
                  />
                  <p>
                    {data.data_quality.awaiting_review > 0 && (
                      <>
                        {data.data_quality.awaiting_review} transactions are awaiting
                        review
                        {data.data_quality.from_untrusted_statements > 0 && ", and "}
                      </>
                    )}
                    {data.data_quality.from_untrusted_statements > 0 && (
                      <>
                        {data.data_quality.from_untrusted_statements} came from
                        statements that did not reconcile
                      </>
                    )}
                    . These figures include them.{" "}
                    <Link href="/review" className="underline">
                      Open the review center
                    </Link>
                    .
                  </p>
                </CardContent>
              </Card>
            )}

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <StatCard
                label="Spending"
                value={data.net_expenses}
                icon={Receipt}
                hint={`${data.expense_transaction_count} transactions`}
              />
              <StatCard label="Income" value={data.income} icon={Banknote} hint="Salary and credits" />
              <StatCard
                label="Net cash flow"
                value={data.net_cash_flow}
                icon={TrendingUp}
                hint="Income less spending"
              />
              <StatCard
                label="Savings rate"
                value={`${Math.round(Number(data.savings_rate) * 100)}%`}
                money={false}
                icon={PiggyBank}
                hint="Share of income kept"
              />
            </div>

            {/* The same orchestrator the Assistant screen uses. Placed above
                the charts because a question is how most people arrive at a
                dashboard — "what did I spend on food" — and making them find
                the answer in a bar chart first is making them do the work. */}
            <Card className="mt-6">
              <CardHeader>
                <CardTitle>Ask about this</CardTitle>
              </CardHeader>
              <CardContent>
                <QueryBox
                  variant="compact"
                  placeholder="How much did I spend on food this month?"
                />
              </CardContent>
            </Card>

            <div className="mt-6 grid gap-6 lg:grid-cols-3">
              <Card className="lg:col-span-2">
                <CardHeader>
                  <CardTitle>Spending and income</CardTitle>
                </CardHeader>
                <CardContent>
                  {trend.data ? <TrendChart data={trend.data} /> : <Skeleton className="h-64 w-full" />}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Where it went</CardTitle>
                </CardHeader>
                <CardContent>
                  {categories.data?.length ? (
                    <CategoryBars data={categories.data} limit={7} />
                  ) : (
                    <p className="text-sm text-muted">Nothing categorised yet.</p>
                  )}
                </CardContent>
              </Card>
            </div>

            <div className="mt-6 grid gap-6 lg:grid-cols-3">
              <Card className="lg:col-span-2">
                <CardHeader>
                  <CardTitle>Day by day</CardTitle>
                </CardHeader>
                <CardContent>
                  {daily.data ? <DailyBars data={daily.data} /> : <Skeleton className="h-32 w-full" />}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Top merchants</CardTitle>
                </CardHeader>
                <CardContent>
                  <ul className="divide-y divide-border">
                    {(merchants.data ?? []).map((row) => (
                      <li key={row.merchant} className="flex items-center gap-3 py-2 text-sm">
                        <Link
                          href={`/transactions?merchant=${encodeURIComponent(row.merchant)}`}
                          className="flex-1 truncate hover:underline"
                        >
                          {row.merchant}
                        </Link>
                        <Money value={row.total} className="text-sm" />
                      </li>
                    ))}
                    {!merchants.data?.length && (
                      <li className="py-2 text-sm text-muted">No merchants yet.</li>
                    )}
                  </ul>
                </CardContent>
              </Card>
            </div>

            {anomalies.data && anomalies.data.length > 0 && (
              <Card className="mt-6">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    Worth a look
                    <Badge variant="neutral">statistical outliers</Badge>
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {/* Never called fraud. Each carries the numbers behind it. */}
                  <ul className="divide-y divide-border">
                    {anomalies.data.map((item) => (
                      <li key={item.id} className="py-2.5 text-sm">
                        <p>{item.reason}</p>
                        <p className="mt-0.5 text-xs text-muted">
                          {formatDate(item.detected_on)}
                          {item.category_name && ` · ${item.category_name}`}
                        </p>
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>
            )}
          </>
        )
      )}
    </>
  );
}
