"use client";

import { AlertTriangle, CheckCircle2, HelpCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * The four confidence dimensions.
 *
 * A single blended score would hide the failure that matters: a perfectly
 * categorised transaction sitting on a misread amount averages out to
 * "probably fine". So the gate is min(), and this component shows all four
 * and names the weakest — a reviewer needs to know *what* to check, not just
 * that something is off.
 */
export type Confidence = {
  extraction: number;
  merchant: number;
  category: number;
  validation: number;
};

export const AUTO_APPROVE_THRESHOLD = 0.97;
export const REVIEW_THRESHOLD = 0.9;

const DIMENSION_LABELS: Record<keyof Confidence, string> = {
  extraction: "Extraction",
  merchant: "Merchant",
  category: "Category",
  validation: "Validation",
};

const DIMENSION_QUESTIONS: Record<keyof Confidence, string> = {
  extraction: "Did we read this row off the statement correctly?",
  merchant: "Did we work out who this payment was to?",
  category: "Is the assigned category right?",
  validation: "Does the statement it came from reconcile?",
};

export function minConfidence(confidence: Confidence): number {
  return Math.min(
    confidence.extraction,
    confidence.merchant,
    confidence.category,
    confidence.validation,
  );
}

export function weakestDimension(confidence: Confidence): keyof Confidence {
  return (Object.keys(DIMENSION_LABELS) as (keyof Confidence)[]).reduce((weakest, key) =>
    confidence[key] < confidence[weakest] ? key : weakest,
  );
}

export type ReviewStatus = "auto_approved" | "flagged" | "review_required" | "resolved";

export function reviewStatusFor(confidence: Confidence): ReviewStatus {
  const score = minConfidence(confidence);
  if (score >= AUTO_APPROVE_THRESHOLD) return "auto_approved";
  if (score >= REVIEW_THRESHOLD) return "flagged";
  return "review_required";
}

function toneFor(score: number) {
  if (score >= AUTO_APPROVE_THRESHOLD) return "success" as const;
  if (score >= REVIEW_THRESHOLD) return "warning" as const;
  return "error" as const;
}

const TONE_BAR: Record<"success" | "warning" | "error", string> = {
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-error",
};

export function ConfidenceBars({
  confidence,
  className,
  showLabels = true,
}: {
  confidence: Confidence;
  className?: string;
  showLabels?: boolean;
}) {
  const weakest = weakestDimension(confidence);

  return (
    <div className={cn("space-y-2", className)}>
      {(Object.keys(DIMENSION_LABELS) as (keyof Confidence)[]).map((key) => {
        const score = confidence[key];
        const tone = toneFor(score);
        const isWeakest = key === weakest;

        return (
          <Tooltip key={key}>
            <TooltipTrigger asChild>
              <div className="flex items-center gap-3 text-xs">
                {showLabels && (
                  <span
                    className={cn(
                      "w-20 shrink-0 text-left",
                      isWeakest ? "font-medium text-foreground" : "text-muted",
                    )}
                  >
                    {DIMENSION_LABELS[key]}
                  </span>
                )}
                <div
                  className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-sunken"
                  role="meter"
                  aria-valuenow={Math.round(score * 100)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`${DIMENSION_LABELS[key]} confidence`}
                >
                  <div
                    className={cn("h-full rounded-full", TONE_BAR[tone])}
                    style={{ width: `${Math.max(score * 100, 2)}%` }}
                  />
                </div>
                <span
                  data-slot="amount"
                  className={cn(
                    "w-10 shrink-0 text-right",
                    isWeakest ? "font-medium text-foreground" : "text-muted",
                  )}
                >
                  {Math.round(score * 100)}%
                </span>
              </div>
            </TooltipTrigger>
            <TooltipContent>{DIMENSION_QUESTIONS[key]}</TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );
}

/** Compact single-chip form for dense table rows. */
export function ConfidenceChip({ confidence }: { confidence: Confidence }) {
  const score = minConfidence(confidence);
  const status = reviewStatusFor(confidence);
  const weakest = weakestDimension(confidence);

  const config = {
    auto_approved: { variant: "success" as const, Icon: CheckCircle2, label: "Verified" },
    flagged: { variant: "warning" as const, Icon: AlertTriangle, label: "Flagged" },
    review_required: { variant: "error" as const, Icon: HelpCircle, label: "Review" },
    resolved: { variant: "info" as const, Icon: CheckCircle2, label: "Resolved" },
  }[status];

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span>
          <Badge variant={config.variant}>
            <config.Icon className="size-3" />
            {config.label}
            <span data-slot="amount" className="opacity-70">
              {Math.round(score * 100)}%
            </span>
          </Badge>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        Weakest dimension: {DIMENSION_LABELS[weakest]} —{" "}
        {DIMENSION_QUESTIONS[weakest].toLowerCase()}
      </TooltipContent>
    </Tooltip>
  );
}
