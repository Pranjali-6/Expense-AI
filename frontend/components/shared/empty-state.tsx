import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

type EmptyStateProps = {
  icon?: LucideIcon;
  title: string;
  /** Say what the user can do next, not merely that there is nothing here. */
  description: string;
  action?: React.ReactNode;
  className?: string;
  tone?: "neutral" | "info" | "warning";
};

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
  tone = "neutral",
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-border px-6 py-14 text-center",
        tone === "info" && "border-info/40 bg-info-subtle/30",
        tone === "warning" && "border-warning/40 bg-warning-subtle/30",
        className,
      )}
    >
      {Icon && (
        <div
          className={cn(
            "mb-4 grid size-11 place-items-center rounded-full",
            tone === "neutral" && "bg-surface-sunken text-subtle",
            tone === "info" && "bg-info-subtle text-info-text",
            tone === "warning" && "bg-warning-subtle text-warning-text",
          )}
        >
          <Icon className="size-5" aria-hidden="true" />
        </div>
      )}
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      <p className="mt-1 max-w-sm text-sm text-muted">{description}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

/** Used on every not-yet-implemented route so a skeleton screen still explains
 *  itself rather than looking broken. */
export function PhasePlaceholder({
  title,
  description,
  phase,
}: {
  title: string;
  description: string;
  phase: string;
}) {
  return (
    <div className="rounded-lg border border-dashed border-border bg-surface-sunken/40 px-6 py-12 text-center">
      <p className="text-xs font-medium uppercase tracking-wide text-primary-text">
        Arrives in {phase}
      </p>
      <h3 className="mt-2 text-sm font-semibold text-foreground">{title}</h3>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted">{description}</p>
    </div>
  );
}
