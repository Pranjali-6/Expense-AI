import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Badges carry state, so every variant pairs a subtle tinted background with a
 * darkened (light mode) or lightened (dark mode) text token. Tinted background
 * plus the raw brand colour as text would fail contrast in one theme or the
 * other; the `-text` tokens exist precisely for this.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
  {
    variants: {
      variant: {
        neutral: "border-border bg-surface-sunken text-muted",
        success: "border-transparent bg-success-subtle text-success-text",
        warning: "border-transparent bg-warning-subtle text-warning-text",
        error: "border-transparent bg-error-subtle text-error-text",
        info: "border-transparent bg-info-subtle text-info-text",
        primary: "border-transparent bg-primary-subtle text-primary-text",
        accent: "border-transparent bg-accent-subtle text-accent-text",
        outline: "border-border-strong bg-transparent text-foreground",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
