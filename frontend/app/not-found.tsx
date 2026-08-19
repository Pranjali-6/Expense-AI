import Link from "next/link";
import { Compass } from "lucide-react";

import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="grid min-h-dvh place-items-center px-6">
      <div className="max-w-md text-center">
        <span className="mx-auto grid size-12 place-items-center rounded-full bg-surface-sunken text-subtle">
          <Compass className="size-5" aria-hidden="true" />
        </span>
        <h1 className="mt-5 text-lg font-semibold tracking-tight">Page not found</h1>
        <p className="mt-2 text-sm text-muted">
          That page does not exist. It may have moved, or the link may be
          incomplete.
        </p>
        <Button variant="primary" asChild className="mt-6">
          <Link href="/dashboard">Back to dashboard</Link>
        </Button>
      </div>
    </div>
  );
}
