"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Target, Trash2 } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Money } from "@/components/shared/money";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { budgets } from "@/lib/api";
import { cn } from "@/lib/utils";

const CATEGORIES = [
  "food", "grocery", "rent", "utilities", "shopping", "travel", "fuel",
  "entertainment", "subscriptions", "healthcare", "insurance", "education",
  "emi", "bank_charges", "taxes", "other",
];

const STATE_STYLE: Record<string, { bar: string; badge: "success" | "warning" | "error"; label: string }> = {
  on_track: { bar: "bg-success", badge: "success", label: "On track" },
  warning: { bar: "bg-warning", badge: "warning", label: "Close to limit" },
  exceeded: { bar: "bg-error", badge: "error", label: "Over budget" },
};

export default function BudgetsPage() {
  const queryClient = useQueryClient();
  const [slug, setSlug] = React.useState("food");
  const [amount, setAmount] = React.useState("");

  const { data, isLoading } = useQuery({ queryKey: ["budgets"], queryFn: () => budgets.list() });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["budgets"] });

  const create = useMutation({
    mutationFn: () => budgets.create({ category_slug: slug, amount }),
    onSuccess: () => {
      setAmount("");
      invalidate();
    },
  });
  const remove = useMutation({ mutationFn: budgets.remove, onSuccess: invalidate });

  return (
    <>
      <PageHeader
        title="Budgets"
        description="What you meant to spend, against what you did. Projections are arithmetic, and are marked as such."
      />

      <Card className="mb-6">
        <CardContent className="flex flex-wrap items-end gap-3 p-4">
          <div>
            <label htmlFor="budget-category" className="mb-1 block text-xs text-muted">
              Category
            </label>
            <select
              id="budget-category"
              className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
            >
              {CATEGORIES.map((value) => (
                <option key={value} value={value}>
                  {value.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="budget-amount" className="mb-1 block text-xs text-muted">
              Monthly limit (₹)
            </label>
            <Input
              id="budget-amount"
              inputMode="decimal"
              placeholder="10000"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              className="w-40"
            />
          </div>
          <Button
            variant="primary"
            disabled={!amount || create.isPending}
            onClick={() => create.mutate()}
          >
            <Plus className="size-4" />
            Set budget
          </Button>
        </CardContent>
      </Card>

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      ) : !data?.length ? (
        <EmptyState
          icon={Target}
          title="No budgets set"
          description="Set a monthly limit for a category and this page tracks it against real spending, with a projection for where the month is heading."
        />
      ) : (
        <div className="space-y-4">
          {data.map((row) => {
            const share = Math.min(Number(row.share_used), 1);
            // Fall back rather than assert: an unexpected state from the API
            // should render as neutral, not crash the page.
            const style = STATE_STYLE[row.state] ?? STATE_STYLE.on_track!;
            return (
              <Card key={row.id}>
                <CardContent className="p-5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="font-semibold">{row.category_name}</h3>
                      <p className="text-xs text-muted">
                        <Money value={row.spent} className="text-xs" /> of{" "}
                        <Money value={row.amount} className="text-xs" /> ·{" "}
                        day {row.days_elapsed} of {row.days_in_month}
                      </p>
                    </div>
                    <Badge variant={style.badge}>{style.label}</Badge>
                  </div>

                  <div
                    className="mt-3 h-2 w-full overflow-hidden rounded-full bg-surface-sunken"
                    role="meter"
                    aria-valuenow={Math.round(share * 100)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`${row.category_name} budget used`}
                  >
                    <div
                      className={cn("h-full rounded-full", style.bar)}
                      style={{ width: `${Math.max(share * 100, 1)}%` }}
                    />
                  </div>

                  <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-muted">
                    <span>
                      {Number(row.remaining) >= 0 ? "Left: " : "Over by: "}
                      <Money
                        value={String(Math.abs(Number(row.remaining)))}
                        className="text-xs"
                      />
                    </span>
                    <span>
                      Projected month-end:{" "}
                      <Money value={row.projected_total} className="text-xs" />
                      {!row.projection_reliable && (
                        // Early in a month a run rate is dominated by whichever
                        // days happened first. Said, not hidden.
                        <span className="ml-1 text-warning-text">
                          (early estimate)
                        </span>
                      )}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-auto"
                      aria-label={`Delete the ${row.category_name} budget`}
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(row.id)}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}
