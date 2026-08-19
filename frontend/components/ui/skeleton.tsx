import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn("skeleton-shimmer rounded-md", className)}
      {...props}
    />
  );
}

/** Placeholder for a metric tile — matches StatCard's real dimensions so the
 *  layout doesn't jump when data arrives. */
function StatSkeleton() {
  return (
    <div className="rounded-lg border border-border bg-surface p-5">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="mt-3 h-7 w-32" />
      <Skeleton className="mt-3 h-3 w-20" />
    </div>
  );
}

/** Placeholder for a ledger. Rows are staggered slightly so it reads as a list
 *  rather than a solid grey block. */
function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="divide-y divide-border">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="flex items-center gap-4 px-4 py-3">
          <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3" style={{ width: `${45 + ((index * 7) % 30)}%` }} />
            <Skeleton className="h-2 w-24" />
          </div>
          <Skeleton className="h-4 w-20 shrink-0" />
        </div>
      ))}
    </div>
  );
}

function ChartSkeleton({ className }: { className?: string }) {
  return <Skeleton className={cn("h-64 w-full", className)} />;
}

export { Skeleton, StatSkeleton, TableSkeleton, ChartSkeleton };
