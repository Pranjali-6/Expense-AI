"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ShieldCheck } from "lucide-react";

import { NAV_SECTIONS } from "@/components/layout/nav-config";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

function isActive(pathname: string, href: string) {
  if (pathname === href) return true;
  // /statements must not light up while on /statements/health, which has its
  // own entry.
  const deeperMatch = NAV_SECTIONS.flatMap((section) => section.items).some(
    (item) => item.href !== href && item.href.startsWith(`${href}/`) && pathname.startsWith(item.href),
  );
  return !deeperMatch && pathname.startsWith(`${href}/`);
}

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <nav className="flex-1 space-y-6 overflow-y-auto px-3 py-4" aria-label="Main">
      {NAV_SECTIONS.map((section) => (
        <div key={section.title}>
          <p className="px-3 pb-2 text-[0.6875rem] font-semibold uppercase tracking-wider text-subtle">
            {section.title}
          </p>
          <ul className="space-y-0.5">
            {section.items.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "group flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                      active
                        ? "bg-primary-subtle font-medium text-primary-text"
                        : "text-muted hover:bg-surface-sunken hover:text-foreground",
                    )}
                  >
                    <item.icon
                      className={cn("size-4 shrink-0", active ? "text-primary-text" : "text-subtle group-hover:text-foreground")}
                      aria-hidden="true"
                    />
                    <span className="truncate">{item.label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

export function SidebarBrand() {
  return (
    <Link href="/dashboard" className="flex items-center gap-2.5 px-5 py-4">
      <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground">
        <ShieldCheck className="size-4" aria-hidden="true" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold tracking-tight">
          Expense AI
        </span>
        <span className="block truncate text-[0.6875rem] text-subtle">
          Financial intelligence
        </span>
      </span>
    </Link>
  );
}

function SidebarFooter() {
  return (
    <div className="border-t border-border p-3">
      <Link
        href="/privacy"
        className="flex items-start gap-2.5 rounded-md p-2.5 text-xs hover:bg-surface-sunken"
      >
        <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-success-text" aria-hidden="true" />
        <span className="text-muted">
          Statements are parsed locally.{" "}
          <span className="font-medium text-foreground">No PDF or account number</span>{" "}
          is ever sent to an AI model.
          <Badge variant="success" className="mt-2 flex w-fit">
            Privacy Center
          </Badge>
        </span>
      </Link>
    </div>
  );
}

export function Sidebar() {
  return (
    // sticky + h-dvh, not just flex-col: otherwise the sidebar stretches with
    // the page and its footer ends up below the fold on a long screen.
    <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 flex-col border-r border-border bg-surface lg:flex">
      <SidebarBrand />
      <SidebarNav />
      <SidebarFooter />
    </aside>
  );
}
