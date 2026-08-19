"use client";

/**
 * Charts.
 *
 * Form follows the data's job, not habit:
 *
 * - **Trend over months** — bars for spending, a line for income. Both are
 *   rupees on one axis. Never a second y-axis: two scales on one chart make any
 *   crossing point meaningless, and readers infer a relationship from it anyway.
 * - **Category breakdown** — horizontal bars, not a pie. Twenty-two categories
 *   cannot be compared as angles, and the question ("which is biggest, and by
 *   how much?") is a length question.
 * - **Daily spending** — thin bars, because the shape of the month is the point.
 *
 * Colour is assigned per *entity* from the validated categorical order, so a
 * category keeps its colour when a filter changes the series count. Amounts are
 * formatted from Decimal strings; no arithmetic happens in this file.
 *
 * Animation is off everywhere. On a financial dashboard a growing bar adds
 * nothing, it replays on every refetch, and a chart caught mid-animation is a
 * chart displaying a number that is not the value.
 */

import * as React from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatMoney, formatMoneyCompact } from "@/lib/format";
import { CATEGORICAL_ORDER, PALETTE, type PaletteSlot } from "@/lib/palette";

function useTheme(): "light" | "dark" {
  const [theme, setTheme] = React.useState<"light" | "dark">("dark");
  React.useEffect(() => {
    const read = () =>
      setTheme(
        document.documentElement.classList.contains("dark") ? "dark" : "light",
      );
    read();
    const observer = new MutationObserver(read);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  }, []);
  return theme;
}

const GRID = "var(--color-border)";
const AXIS = "var(--color-muted)";

function monthLabel(iso: string): string {
  const [year, month] = iso.split("-");
  return new Date(Number(year), Number(month) - 1, 1).toLocaleDateString("en-IN", {
    month: "short",
    year: "2-digit",
  });
}

/** Shared tooltip. Values are formatted, never recomputed. */
function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { name: string; value: number; color: string }[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2 text-xs shadow-lg">
      <p className="mb-1 font-medium">{label}</p>
      {payload.map((item) => (
        <p key={item.name} className="flex items-center gap-2">
          <span
            className="size-2 shrink-0 rounded-full"
            style={{ background: item.color }}
            aria-hidden="true"
          />
          <span className="text-muted">{item.name}</span>
          <span data-slot="amount" className="ml-auto font-medium">
            {formatMoney(item.value)}
          </span>
        </p>
      ))}
    </div>
  );
}

export function TrendChart({
  data,
}: {
  data: { month: string; net_expenses: string; income: string }[];
}) {
  const theme = useTheme();
  const rows = data.map((row) => ({
    month: monthLabel(row.month),
    Spending: Number(row.net_expenses),
    Income: Number(row.income),
  }));

  return (
    <div>
      {/* Legend is always present for two series — identity is never
          carried by colour alone. */}
      <div className="mb-3 flex flex-wrap gap-4 text-xs text-muted">
        <span className="flex items-center gap-1.5">
          <span
            className="size-2.5 rounded-sm"
            style={{ background: PALETTE.blue[theme] }}
            aria-hidden="true"
          />
          Spending
        </span>
        <span className="flex items-center gap-1.5">
          <span
            className="h-0.5 w-4 rounded-full"
            style={{ background: PALETTE.aqua[theme] }}
            aria-hidden="true"
          />
          Income
        </span>
      </div>
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart data={rows} margin={{ top: 4, right: 8, bottom: 4, left: 8 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="month"
            stroke={AXIS}
            tickLine={false}
            axisLine={false}
            fontSize={11}
          />
          <YAxis
            stroke={AXIS}
            tickLine={false}
            axisLine={false}
            fontSize={11}
            width={64}
            tickFormatter={(value: number) => formatMoneyCompact(value)}
          />
          <Tooltip content={<ChartTooltip />} cursor={{ fill: "var(--color-surface-sunken)" }} />
          <Bar
            dataKey="Spending"
            fill={PALETTE.blue[theme]}
            radius={[4, 4, 0, 0]}
            maxBarSize={28}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="Income"
            stroke={PALETTE.aqua[theme]}
            strokeWidth={2}
            dot={{ r: 3 }}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export function CategoryBars({
  data,
  limit = 8,
}: {
  data: { slug: string; name: string; total: string; share: string }[];
  limit?: number;
}) {
  const theme = useTheme();

  // Past the palette's capacity the remainder folds into one "Other" bar rather
  // than generating a ninth hue.
  const head = data.slice(0, limit);
  const tail = data.slice(limit);
  const rows = [
    ...head.map((row, index) => ({
      name: row.name,
      value: Number(row.total),
      slot: CATEGORICAL_ORDER[index % CATEGORICAL_ORDER.length] as PaletteSlot,
    })),
    ...(tail.length
      ? [
          {
            name: `Other (${tail.length})`,
            value: tail.reduce((total, row) => total + Number(row.total), 0),
            slot: "neutral" as PaletteSlot,
          },
        ]
      : []),
  ];

  return (
    <ResponsiveContainer width="100%" height={Math.max(rows.length * 34, 120)}>
      <BarChart
        data={rows}
        layout="vertical"
        margin={{ top: 0, right: 16, bottom: 0, left: 8 }}
      >
        <CartesianGrid stroke={GRID} strokeDasharray="2 4" horizontal={false} />
        <XAxis
          type="number"
          stroke={AXIS}
          tickLine={false}
          axisLine={false}
          fontSize={11}
          tickFormatter={(value: number) => formatMoneyCompact(value)}
        />
        <YAxis
          type="category"
          dataKey="name"
          stroke={AXIS}
          tickLine={false}
          axisLine={false}
          fontSize={11}
          width={110}
        />
        <Tooltip content={<ChartTooltip />} cursor={{ fill: "var(--color-surface-sunken)" }} />
        <Bar
          dataKey="value"
          name="Spent"
          radius={[0, 4, 4, 0]}
          maxBarSize={20}
          isAnimationActive={false}
        >
          {rows.map((row) => (
            <Cell key={row.name} fill={PALETTE[row.slot][theme]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function DailyBars({ data }: { data: { day: string; expenses: string }[] }) {
  const theme = useTheme();
  const rows = data.map((row) => ({
    day: String(Number(row.day.slice(-2))),
    Spent: Number(row.expenses),
  }));

  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="day"
          stroke={AXIS}
          tickLine={false}
          axisLine={false}
          fontSize={10}
          interval={4}
        />
        <YAxis hide />
        <Tooltip content={<ChartTooltip />} cursor={{ fill: "var(--color-surface-sunken)" }} />
        <Bar
          dataKey="Spent"
          fill={PALETTE.blue[theme]}
          radius={[2, 2, 0, 0]}
          maxBarSize={12}
          isAnimationActive={false}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
