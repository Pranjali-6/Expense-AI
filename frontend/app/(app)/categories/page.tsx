"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Shapes, Trash2 } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Money } from "@/components/shared/money";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { categories, type CategoryRule } from "@/lib/api";
import { formatDate } from "@/lib/format";

function RuleRow({ rule, onDelete }: { rule: CategoryRule; onDelete: () => void }) {
  return (
    <li className="flex flex-wrap items-center gap-3 py-3 text-sm">
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">
          {rule.match_type === "contains" ? "Contains " : ""}
          &ldquo;{rule.merchant_pattern}&rdquo; → {rule.category_name}
          {rule.subcategory_name && ` · ${rule.subcategory_name}`}
        </span>
        <span className="block text-xs text-muted">
          Created {formatDate(rule.created_at)}
          {" · "}
          {rule.times_applied === 1
            ? "applied once"
            : `applied ${rule.times_applied} times`}
          {rule.min_amount && ` · from ₹${rule.min_amount}`}
          {rule.max_amount && ` · up to ₹${rule.max_amount}`}
        </span>
      </span>
      <Button
        variant="ghost"
        size="sm"
        aria-label={`Delete the rule for ${rule.merchant_pattern}`}
        onClick={onDelete}
      >
        <Trash2 className="size-4" />
      </Button>
    </li>
  );
}

export default function CategoriesPage() {
  const queryClient = useQueryClient();
  const [pattern, setPattern] = React.useState("");
  const [slug, setSlug] = React.useState("food");
  const [matchType, setMatchType] = React.useState<"exact" | "contains">("exact");

  const list = useQuery({ queryKey: ["categories"], queryFn: categories.list });
  const rules = useQuery({ queryKey: ["category-rules"], queryFn: categories.rules });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["category-rules"] });
  };

  const create = useMutation({
    mutationFn: categories.createRule,
    onSuccess: () => {
      setPattern("");
      invalidate();
    },
  });
  const remove = useMutation({ mutationFn: categories.deleteRule, onSuccess: invalidate });

  const used = (list.data ?? []).filter((row) => row.transaction_count > 0);

  return (
    <>
      <PageHeader
        title="Categories"
        description="The fixed taxonomy, and the rules you have set that outrank everything else."
      />

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Your rules</CardTitle>
          </CardHeader>
          <CardContent>
            {/* Stated plainly, because it is the reason to create one. */}
            <p className="mb-4 text-sm text-muted">
              A rule sits at the top of the cascade — above the merchant
              dictionary, above the built-in rules, above any AI suggestion. It
              applies to future imports; transactions already in your ledger are
              left exactly as they are.
            </p>

            <form
              className="mb-4 flex flex-wrap items-end gap-2 rounded-lg border border-border bg-surface-sunken/40 p-3"
              onSubmit={(event) => {
                event.preventDefault();
                if (!pattern.trim()) return;
                create.mutate({
                  merchant_pattern: pattern.trim(),
                  category_slug: slug,
                  match_type: matchType,
                });
              }}
            >
              <label className="min-w-[180px] flex-1 text-xs text-muted">
                Merchant
                <Input
                  className="mt-1"
                  value={pattern}
                  onChange={(event) => setPattern(event.target.value)}
                  placeholder="Swiggy"
                  maxLength={255}
                />
              </label>
              <label className="text-xs text-muted">
                Match
                <select
                  className="mt-1 block h-9 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
                  value={matchType}
                  onChange={(event) =>
                    setMatchType(event.target.value as "exact" | "contains")
                  }
                >
                  <option value="exact">is exactly</option>
                  <option value="contains">contains</option>
                </select>
              </label>
              <label className="text-xs text-muted">
                Category
                <select
                  className="mt-1 block h-9 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
                  value={slug}
                  onChange={(event) => setSlug(event.target.value)}
                >
                  {(list.data ?? []).map((row) => (
                    <option key={row.slug} value={row.slug}>
                      {row.name}
                    </option>
                  ))}
                </select>
              </label>
              <Button type="submit" variant="primary" disabled={create.isPending}>
                <Plus className="size-4" />
                Add rule
              </Button>
            </form>

            {create.isError && (
              <p className="mb-3 text-sm text-error-text">
                {(create.error as Error).message}
              </p>
            )}

            {rules.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : !rules.data?.length ? (
              <p className="py-6 text-center text-sm text-muted">
                No rules yet. Correcting a transaction&rsquo;s category creates one
                automatically.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {rules.data.map((rule) => (
                  <RuleRow
                    key={rule.id}
                    rule={rule}
                    onDelete={() => remove.mutate(rule.id)}
                  />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>In use</CardTitle>
          </CardHeader>
          <CardContent>
            {list.isLoading ? (
              <Skeleton className="h-64 w-full" />
            ) : !used.length ? (
              <EmptyState
                icon={Shapes}
                title="Nothing categorised yet"
                description="Import a statement and categories fill in automatically."
              />
            ) : (
              <ul className="divide-y divide-border">
                {used.map((row) => (
                  <li key={row.slug} className="flex items-center gap-2 py-2 text-sm">
                    <Link
                      href={`/transactions?category=${row.slug}`}
                      className="min-w-0 flex-1 truncate hover:underline"
                    >
                      {row.name}
                    </Link>
                    <Badge variant="neutral">{row.transaction_count}</Badge>
                    {row.is_expense && (
                      <Money value={row.total} className="text-xs" />
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
