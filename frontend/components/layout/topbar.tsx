"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Bell, Upload } from "lucide-react";

import { findNavItem } from "@/components/layout/nav-config";
import { MobileMenuButton } from "@/components/layout/mobile-nav";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { UserMenu } from "@/components/layout/user-menu";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { notifications } from "@/lib/api";

export function Topbar() {
  const pathname = usePathname();
  const current = findNavItem(pathname);

  // Polled rather than pushed. Notifications arrive from background workers,
  // so there is nothing in the request/response cycle to piggyback on, and a
  // second SSE stream purely for a badge is not worth a held connection.
  const unread = useQuery({
    queryKey: ["notifications"],
    queryFn: () => notifications.list(),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
  const count = unread.data?.unread ?? 0;

  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b border-border bg-surface/95 px-3 backdrop-blur sm:px-5">
      <MobileMenuButton />

      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-foreground lg:hidden">
          {current?.label ?? "Expense AI"}
        </p>
        <p className="hidden truncate text-sm text-muted lg:block">
          {current?.description ?? ""}
        </p>
      </div>

      <Button variant="primary" size="sm" asChild className="hidden sm:inline-flex">
        <Link href="/upload">
          <Upload className="size-4" />
          Upload statements
        </Link>
      </Button>

      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            asChild
            aria-label={
              count ? `Notifications, ${count} unread` : "Notifications"
            }
          >
            <Link href="/notifications" className="relative">
              <Bell className="size-4" />
              {count > 0 && (
                <span
                  className="absolute right-1 top-1 grid min-w-4 place-items-center rounded-full bg-primary px-1 text-[10px] font-semibold leading-4 text-primary-foreground"
                  aria-hidden="true"
                >
                  {count > 9 ? "9+" : count}
                </span>
              )}
            </Link>
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          {count ? `${count} unread` : "Notifications"}
        </TooltipContent>
      </Tooltip>

      <ThemeToggle />

      <UserMenu />
    </header>
  );
}
