import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Money } from "@/components/shared/money";
import { cn } from "@/lib/utils";
import { formatPercent } from "@/lib/format";

type Delta = {
  /** Percent change against the comparison period. */
  percent: number;
  label: string;
  /**
   * Whether an increase is good. Spending up is bad; income up is good.
   * Without this a dashboard cheerfully paints a spending spike green.
   */
  increaseIsGood?: boolean;
};

type StatCardProps = {
  label: string;
  value: string | number | null | undefined;
  /** Render `value` as rupees. Set false for counts, ratios, percentages. */
  money?: boolean;
  hint?: string;
  icon?: LucideIcon;
  delta?: Delta;
  className?: string;
};

export function StatCard({
  label,
  value,
  money = true,
  hint,
  icon: Icon,
  delta,
  className,
}: StatCardProps) {
  const direction = delta ? (delta.percent > 0 ? "up" : delta.percent < 0 ? "down" : "flat") : null;
  const increaseIsGood = delta?.increaseIsGood ?? false;
  const isGood =
    direction === "flat" || direction === null
      ? null
      : (direction === "up") === increaseIsGood;

  const DeltaIcon = direction === "up" ? ArrowUpRight : direction === "down" ? ArrowDownRight : Minus;

  return (
    <Card className={cn("p-5", className)}>
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium uppercase tracking-wide text-muted">{label}</p>
        {Icon && <Icon className="size-4 shrink-0 text-subtle" aria-hidden="true" />}
      </div>

      <div className="mt-3">
        {money ? (
          <Money value={value} emphasis="display" />
        ) : (
          <span data-slot="amount" className="text-2xl font-semibold tracking-tight">
            {value ?? "—"}
          </span>
        )}
      </div>

      {(delta || hint) && (
        <div className="mt-3 flex items-center gap-2 text-xs">
          {delta && (
            <span
              className={cn(
                "inline-flex items-center gap-0.5 font-medium",
                isGood === null && "text-muted",
                isGood === true && "text-success-text",
                isGood === false && "text-error-text",
              )}
            >
              <DeltaIcon className="size-3.5" aria-hidden="true" />
              {formatPercent(Math.abs(delta.percent))}
            </span>
          )}
          <span className="text-muted">{delta?.label ?? hint}</span>
        </div>
      )}
    </Card>
  );
}
