/**
 * Formatting helpers.
 *
 * Money arrives from the API as a **string**, because the backend stores it as
 * NUMERIC and serialises Decimal without going through a float. That contract
 * would be pointless if the client immediately did `parseFloat()`, so the
 * currency formatter here works on the string directly: it groups digits in the
 * Indian style (last three, then pairs) without ever constructing a Number.
 *
 * `Intl.NumberFormat('en-IN')` would produce the same grouping, but only after
 * a lossy conversion. This costs a dozen lines and keeps the invariant intact
 * end to end.
 */

/** Indian digit grouping: 1,23,45,678.90 — last three, then pairs. */
function groupIndian(integerDigits: string): string {
  if (integerDigits.length <= 3) return integerDigits;

  const lastThree = integerDigits.slice(-3);
  const rest = integerDigits.slice(0, -3);
  const grouped = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",");

  return `${grouped},${lastThree}`;
}

type MoneyOptions = {
  /** Render the ₹ symbol. Default true. */
  symbol?: boolean;
  /** Force a leading + or −. Default false. */
  signed?: boolean;
  /** Drop the paise when they are .00. Default false — ledgers show them. */
  compactPaise?: boolean;
};

/**
 * Format a decimal string as Indian rupees.
 *
 *   formatMoney("1234567.5")   → "₹12,34,567.50"
 *   formatMoney("-450", {signed: true}) → "−₹450.00"
 */
export function formatMoney(
  value: string | number | null | undefined,
  options: MoneyOptions = {},
): string {
  const { symbol = true, signed = false, compactPaise = false } = options;

  if (value === null || value === undefined || value === "") return symbol ? "₹—" : "—";

  const raw = String(value).trim();
  const negative = raw.startsWith("-");
  const unsigned = raw.replace(/^[+-]/, "");

  const [integerPart = "0", fractionPart = ""] = unsigned.split(".");
  const paise = (fractionPart + "00").slice(0, 2);

  const digits = integerPart.replace(/\D/g, "") || "0";
  const grouped = groupIndian(digits);

  const showPaise = !(compactPaise && paise === "00");
  const amount = showPaise ? `${grouped}.${paise}` : grouped;

  const prefix = negative ? "−" : signed ? "+" : "";
  return `${prefix}${symbol ? "₹" : ""}${amount}`;
}

/** Short form for chart axes and dense tiles: ₹12.3L, ₹4.5Cr. */
export function formatMoneyCompact(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "₹—";

  const numeric = Math.abs(Number(value));
  const sign = Number(value) < 0 ? "−" : "";

  if (!Number.isFinite(numeric)) return "₹—";
  if (numeric >= 1_00_00_000) return `${sign}₹${(numeric / 1_00_00_000).toFixed(2)}Cr`;
  if (numeric >= 1_00_000) return `${sign}₹${(numeric / 1_00_000).toFixed(2)}L`;
  if (numeric >= 1_000) return `${sign}₹${(numeric / 1_000).toFixed(1)}K`;

  return `${sign}₹${numeric.toFixed(0)}`;
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

export function formatDate(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(date);
}

export function formatDateTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-IN").format(value);
}

/**
 * Mask an account number for display. The API never sends a full account
 * number — this exists so a masked value renders consistently, and as a last
 * line of defence if one ever slips through.
 */
export function maskAccount(value: string | null | undefined): string {
  if (!value) return "—";
  const digits = value.replace(/\s/g, "");
  if (digits.length <= 4) return `••••${digits}`;
  return `••••${digits.slice(-4)}`;
}
