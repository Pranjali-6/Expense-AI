"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, MoreHorizontal } from "lucide-react";

import {
  MOBILE_NAV_HREFS,
  NAV_ITEMS,
  NAV_SECTIONS,
} from "@/components/layout/nav-config";
import { SidebarBrand, SidebarNav } from "@/components/layout/sidebar";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

/** Hamburger in the top bar — opens the full navigation as a drawer. */
export function MobileMenuButton() {
  const [open, setOpen] = React.useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation">
          <Menu className="size-5" />
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="p-0">
        <SheetTitle className="sr-only">Navigation</SheetTitle>
        <SheetDescription className="sr-only">
          Move between sections of Expense AI
        </SheetDescription>
        <SidebarBrand />
        <SidebarNav onNavigate={() => setOpen(false)} />
      </SheetContent>
    </Sheet>
  );
}

/**
 * Bottom navigation, phones only.
 *
 * Four destinations plus More. Cramming seventeen routes into a thumb-height
 * bar makes all of them harder to hit; the four here are what someone actually
 * opens on a phone — check the position, scan the ledger, add a statement,
 * clear the review queue.
 */
export function MobileBottomNav() {
  const pathname = usePathname();
  const [moreOpen, setMoreOpen] = React.useState(false);

  const primary = MOBILE_NAV_HREFS.map((href) =>
    NAV_ITEMS.find((item) => item.href === href),
  ).filter((item): item is NonNullable<typeof item> => Boolean(item));

  const primaryHrefs = new Set<string>(MOBILE_NAV_HREFS);
  const moreActive = !primaryHrefs.has(pathname);

  return (
    <>
      <nav
        aria-label="Primary"
        className={cn(
          "fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface/95 backdrop-blur",
          "pb-[env(safe-area-inset-bottom)] lg:hidden",
        )}
      >
        <ul className="grid grid-cols-5">
          {primary.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex min-h-14 flex-col items-center justify-center gap-1 px-1 py-2 text-[0.6875rem]",
                    active ? "text-primary-text" : "text-muted",
                  )}
                >
                  <item.icon className="size-5" aria-hidden="true" />
                  <span className="truncate">{item.label}</span>
                </Link>
              </li>
            );
          })}
          <li>
            <button
              type="button"
              onClick={() => setMoreOpen(true)}
              aria-label="More sections"
              className={cn(
                "flex min-h-14 w-full flex-col items-center justify-center gap-1 px-1 py-2 text-[0.6875rem]",
                moreActive ? "text-primary-text" : "text-muted",
              )}
            >
              <MoreHorizontal className="size-5" aria-hidden="true" />
              <span>More</span>
            </button>
          </li>
        </ul>
      </nav>

      <Sheet open={moreOpen} onOpenChange={setMoreOpen}>
        <SheetContent side="bottom" className="max-h-[80vh] overflow-y-auto p-0">
          <SheetTitle className="px-5 pt-5 text-sm font-semibold">All sections</SheetTitle>
          <SheetDescription className="sr-only">
            Every section of Expense AI
          </SheetDescription>
          <div className="grid grid-cols-2 gap-2 p-4 pb-8 sm:grid-cols-3">
            {NAV_SECTIONS.flatMap((section) => section.items).map((item) => (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setMoreOpen(false)}
                className="flex items-center gap-2.5 rounded-md border border-border p-3 text-sm hover:bg-surface-sunken"
              >
                <item.icon className="size-4 shrink-0 text-subtle" aria-hidden="true" />
                <span className="truncate">{item.label}</span>
              </Link>
            ))}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
