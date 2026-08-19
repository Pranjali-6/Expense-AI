"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { CalendarClock, Repeat } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { StatCard } from "@/components/shared/stat-card";
import { Money } from "@/components/shared/money";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { TableSkeleton } from "@/components/ui/skeleton";
import { intelligence } from "@/lib/api";
import { formatDate } from "@/lib/format";

const CADENCE_LABEL: Record<string, string> = {
  weekly: "Weekly",
  fortnightly: "Fortnightly",
  monthly: "Monthly",
  quarterly: "Quarterly",
  half_yearly: "Every 6 months",
  annual: "Yearly",
};

export default function SubscriptionsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["subscriptions"],
    queryFn: intelligence.recurring,
  });

  const active = (data ?? []).filter((row) => row.status === "active");
  const annual = active.reduce((total, row) => total + Number(row.estimated_annual_cost), 0);
  const monthly = annual / 12;

  return (
    <>
      <PageHeader
        title="Subscriptions"
        description="Recurring charges found by looking at the gaps between payments. Nothing was guessed by a model."
      />

      {isLoading ? (
        <Card>
          <TableSkeleton rows={5} />
        </Card>
      ) : !data?.length ? (
        <EmptyState
          icon={Repeat}
          title="No recurring charges found yet"
          description="A charge needs to appear at least three times, at consistent intervals, before it counts as a subscription. Import a few months of statements and they appear here."
          action={
            <Button variant="primary" asChild>
              <Link href="/upload">Upload statements</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="mb-6 grid gap-4 sm:grid-cols-3">
            <StatCard label="Active subscriptions" value={active.length} money={false} />
            <StatCard label="Per month" value={monthly.toFixed(2)} hint="Estimated" />
            <StatCard label="Per year" value={annual.toFixed(2)} hint="Estimated" />
          </div>

          <Card>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th scope="col" className="px-4 py-3 font-medium">Merchant</th>
                    <th scope="col" className="px-4 py-3 font-medium">Cadence</th>
                    <th scope="col" className="px-4 py-3 text-right font-medium">Typical</th>
                    <th scope="col" className="px-4 py-3 text-right font-medium">Per year</th>
                    <th scope="col" className="px-4 py-3 font-medium">Next expected</th>
                    <th scope="col" className="px-4 py-3 font-medium">Confidence</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data.map((row) => (
                    <tr key={row.id} className="hover:bg-surface-sunken/50">
                      <td className="px-4 py-3">
                        <Link
                          href={`/transactions?merchant=${encodeURIComponent(row.merchant)}`}
                          className="font-medium hover:underline"
                        >
                          {row.merchant}
                        </Link>
                        <p className="text-xs text-muted">
                          {row.category_name ?? "Uncategorised"} · seen{" "}
                          {row.occurrence_count} times
                        </p>
                      </td>
                      <td className="px-4 py-3 text-muted">
                        {CADENCE_LABEL[row.cadence] ?? row.cadence}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Money value={row.typical_amount} />
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Money value={row.estimated_annual_cost} />
                      </td>
                      <td className="px-4 py-3 text-muted">
                        {row.status === "lapsed" ? (
                          <Badge variant="neutral">Lapsed</Badge>
                        ) : row.next_expected_on ? (
                          <span className="flex items-center gap-1.5">
                            <CalendarClock className="size-3.5" aria-hidden="true" />
                            {formatDate(row.next_expected_on)}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {/* How consistent the intervals are. A prediction is only
                            as good as the regularity behind it, so it is shown. */}
                        <Badge
                          variant={
                            Number(row.cadence_stability) >= 0.9 ? "success" : "warning"
                          }
                        >
                          {Math.round(Number(row.cadence_stability) * 100)}% regular
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </>
  );
}
