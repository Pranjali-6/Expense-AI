/**
 * The validated categorical palette.
 *
 * Eight hues in a fixed order, each with a step chosen for the light surface and
 * a separate step chosen for the dark one — dark is a selected palette, not an
 * automatic flip. Verified with the palette validator: worst adjacent CVD ΔE 9.1
 * light / 8.4 dark (≥8 target), worst adjacent normal-vision ΔE 19.6 / 19.3
 * (≥15 floor).
 *
 * Three light-mode hues (aqua, yellow, magenta) sit below 3:1 against the light
 * surface. That is a known warning, not an oversight: it obliges every chart
 * using them to carry visible direct labels or a table view. Charts here do.
 *
 * ── Two different jobs, deliberately separated ───────────────────────────────
 *
 * **Category identity** (chips, icons, list rows). A category stores a *slot
 * name*, not a hex, and resolves through here. Identity colour always sits
 * beside the category's name, so it is never load-bearing on its own — which is
 * why 22 categories can share 8 hues without ambiguity.
 *
 * **Chart series.** A chart may show at most eight series. Beyond that, the
 * smallest are folded into "Other" rather than inventing a ninth hue, because a
 * generated hue is exactly where CVD safety quietly breaks. Slot assignment
 * within a chart is stable per entity, never by rank — a filter that drops a
 * series must not repaint the survivors.
 */

export type PaletteSlot =
  | "blue"
  | "orange"
  | "aqua"
  | "yellow"
  | "magenta"
  | "green"
  | "violet"
  | "red"
  | "neutral";

type SlotColors = { light: string; dark: string };

/** Fixed order. Never cycled, never reordered at runtime. */
export const CATEGORICAL_ORDER: readonly PaletteSlot[] = [
  "blue",
  "orange",
  "aqua",
  "yellow",
  "magenta",
  "green",
  "violet",
  "red",
] as const;

export const PALETTE: Record<PaletteSlot, SlotColors> = {
  blue: { light: "#2a78d6", dark: "#3987e5" },
  orange: { light: "#eb6834", dark: "#d95926" },
  aqua: { light: "#1baf7a", dark: "#199e70" },
  yellow: { light: "#eda100", dark: "#c98500" },
  magenta: { light: "#e87ba4", dark: "#d55181" },
  green: { light: "#008300", dark: "#008300" },
  violet: { light: "#4a3aa7", dark: "#9085e9" },
  red: { light: "#e34948", dark: "#e66767" },
  // Reserved for "Other" and for anything deliberately de-emphasised.
  neutral: { light: "#8a8a85", dark: "#9a9a94" },
};

/**
 * Slots that clear the all-pairs gate, for chart forms where every series is
 * compared against every other rather than only its neighbours — scatter,
 * bubble, small multiples. Past three, facet or fold to "Other": the fourth
 * slot puts yellow beside orange, and that pair fails the all-pairs floors.
 */
export const ALL_PAIRS_SAFE_SLOTS = 3;

/** Maximum series before folding the remainder into "Other". */
export const MAX_SERIES = 8;

export function slotColor(slot: PaletteSlot, theme: "light" | "dark"): string {
  return PALETTE[slot][theme];
}

/**
 * Assign palette slots to a fixed list of entity keys.
 *
 * Keyed on the entity, so the same category keeps its colour as filters change
 * the series count. Anything past `MAX_SERIES - 1` is expected to have been
 * folded into "Other" by the caller.
 */
export function assignSlots<T extends string>(keys: readonly T[]): Record<T, PaletteSlot> {
  const assignment = {} as Record<T, PaletteSlot>;
  keys.forEach((key, index) => {
    assignment[key] =
      index < CATEGORICAL_ORDER.length
        ? CATEGORICAL_ORDER[index]!
        : "neutral";
  });
  return assignment;
}

/** Status colours, reserved. Never reused as "series 9". */
export const STATUS = {
  good: { light: "#047857", dark: "#34d399" },
  warning: { light: "#b45309", dark: "#fbbf24" },
  serious: { light: "#c2410c", dark: "#fb923c" },
  critical: { light: "#b91c1c", dark: "#f87171" },
} as const;
