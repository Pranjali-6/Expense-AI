import {
  Activity,
  BarChart3,
  Bell,
  Bot,
  CalendarClock,
  FileText,
  Grid2x2,
  Landmark,
  LayoutDashboard,
  ListChecks,
  Receipt,
  ScrollText,
  Settings,
  ShieldCheck,
  Stethoscope,
  Target,
  Upload,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  /** Shown in the sidebar as a count or state chip; wired up in later phases. */
  badgeKey?: "review" | "notifications";
  description: string;
};

export type NavSection = {
  title: string;
  items: NavItem[];
};

export const NAV_SECTIONS: NavSection[] = [
  {
    title: "Overview",
    items: [
      {
        label: "Dashboard",
        href: "/dashboard",
        icon: LayoutDashboard,
        description: "Monthly position, trends and where the money went",
      },
      {
        label: "Timeline",
        href: "/timeline",
        icon: CalendarClock,
        description: "Your financial year as one chronological story",
      },
      {
        label: "Insights",
        href: "/insights",
        icon: BarChart3,
        description: "Monthly report, growth and savings opportunities",
      },
    ],
  },
  {
    title: "Money",
    items: [
      {
        label: "Transactions",
        href: "/transactions",
        icon: Receipt,
        description: "The full ledger, with source and confidence",
      },
      {
        label: "Review",
        href: "/review",
        icon: ListChecks,
        badgeKey: "review",
        description: "Transactions the system is not confident about",
      },
      {
        label: "Subscriptions",
        href: "/subscriptions",
        icon: Activity,
        description: "Recurring charges and what they cost per year",
      },
      {
        label: "Budgets",
        href: "/budgets",
        icon: Target,
        description: "Category budgets with projected month-end spend",
      },
    ],
  },
  {
    title: "Sources",
    items: [
      {
        label: "Upload",
        href: "/upload",
        icon: Upload,
        description: "Drop statement PDFs — no manual entry, ever",
      },
      {
        label: "Statements",
        href: "/statements",
        icon: FileText,
        description: "Every imported statement and its status",
      },
      {
        label: "Statement Health",
        href: "/statements/health",
        icon: Stethoscope,
        description: "Reconciliation and extraction quality per import",
      },
      {
        label: "Accounts",
        href: "/accounts",
        icon: Landmark,
        description: "Bank accounts and cards, always masked",
      },
    ],
  },
  {
    title: "Intelligence",
    items: [
      {
        label: "Assistant",
        href: "/assistant",
        icon: Bot,
        description: "Ask questions about your own money, read-only",
      },
    ],
  },
  {
    title: "System",
    items: [
      {
        label: "Categories",
        href: "/categories",
        icon: Grid2x2,
        description: "Categories, subcategories and your own rules",
      },
      {
        label: "Privacy",
        href: "/privacy",
        icon: ShieldCheck,
        description: "Exactly what has and has not left this system",
      },
      {
        label: "Notifications",
        href: "/notifications",
        icon: Bell,
        badgeKey: "notifications",
        description: "Imports, budget breaches and anomalies",
      },
      {
        label: "Audit Log",
        href: "/audit",
        icon: ScrollText,
        description: "Every change, who made it and when",
      },
      {
        label: "Settings",
        href: "/settings",
        icon: Settings,
        description: "Profile, security, AI and data controls",
      },
    ],
  },
];

/** Flattened lookup for page headers and breadcrumbs. */
export const NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap((section) => section.items);

/**
 * Mobile bottom navigation — four destinations plus More.
 *
 * These four are the ones a phone user actually needs: check the position,
 * scan the ledger, add statements, clear the review queue. Everything else
 * lives behind More rather than being crammed into a 5-icon bar.
 */
export const MOBILE_NAV_HREFS = ["/dashboard", "/transactions", "/upload", "/review"] as const;

export function findNavItem(pathname: string): NavItem | undefined {
  // Longest match wins, so /statements/health beats /statements.
  return NAV_ITEMS.filter(
    (item) => pathname === item.href || pathname.startsWith(`${item.href}/`),
  ).sort((a, b) => b.href.length - a.href.length)[0];
}
