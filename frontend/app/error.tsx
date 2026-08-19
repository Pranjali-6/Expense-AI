"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    // Only the digest is reported. The message could carry data from a failed
    // response, and an error boundary is not a place to start leaking it.
    console.error("Unhandled UI error", { digest: error.digest });
  }, [error.digest]);

  return (
    <div className="grid min-h-dvh place-items-center px-6">
      <div className="max-w-md text-center">
        <span className="mx-auto grid size-12 place-items-center rounded-full bg-error-subtle text-error-text">
          <AlertTriangle className="size-5" aria-hidden="true" />
        </span>
        <h1 className="mt-5 text-lg font-semibold tracking-tight">Something went wrong</h1>
        <p className="mt-2 text-sm text-muted">
          This screen failed to load. Your data is unaffected &mdash; nothing is
          written until an action succeeds.
        </p>
        {error.digest && (
          <p className="mt-3 text-xs text-subtle">
            Reference <code className="rounded bg-surface-sunken px-1.5 py-0.5">{error.digest}</code>
          </p>
        )}
        <Button variant="primary" onClick={reset} className="mt-6">
          Try again
        </Button>
      </div>
    </div>
  );
}
