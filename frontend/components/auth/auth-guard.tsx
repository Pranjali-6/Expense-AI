"use client";

import * as React from "react";
import { useRouter, usePathname } from "next/navigation";
import { ShieldCheck } from "lucide-react";

import { useAuth } from "@/lib/auth-context";

/**
 * Client-side route guard for the signed-in application shell.
 *
 * This is a **user-experience** control, not a security boundary. Nothing here
 * protects data: every API response is authorised server-side from the access
 * token, and the database enforces tenant isolation regardless of what the
 * browser believes. Bypassing this component gets you an empty shell whose every
 * request returns 401.
 */
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  React.useEffect(() => {
    if (!loading && !user) {
      // Preserve where they were headed so sign-in can return them there.
      const next = pathname && pathname !== "/" ? `?next=${encodeURIComponent(pathname)}` : "";
      router.replace(`/login${next}`);
    }
  }, [loading, user, router, pathname]);

  if (loading) {
    return (
      <div className="grid min-h-dvh place-items-center">
        <div className="flex flex-col items-center gap-3">
          <span className="grid size-10 place-items-center rounded-lg bg-primary text-primary-foreground">
            <ShieldCheck className="size-5" aria-hidden="true" />
          </span>
          <p className="text-sm text-muted">Restoring your session…</p>
        </div>
      </div>
    );
  }

  if (!user) return null;

  return <>{children}</>;
}
