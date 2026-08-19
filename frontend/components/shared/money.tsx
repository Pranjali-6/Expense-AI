import { cn } from "@/lib/utils";
import { formatMoney } from "@/lib/format";

type MoneyProps = {
  /** Decimal string from the API. Never a float. */
  value: string | number | null | undefined;
  direction?: "debit" | "credit";
  className?: string;
  signed?: boolean;
  compactPaise?: boolean;
  /** Larger, heavier treatment for headline figures. */
  emphasis?: "normal" | "strong" | "display";
};

/**
 * Renders a rupee amount.
 *
 * `data-slot="amount"` picks up tabular numerals from globals.css, so amounts
 * align in a column without every table having to remember to ask for it.
 */
export function Money({
  value,
  direction,
  className,
  signed = false,
  compactPaise = false,
  emphasis = "normal",
}: MoneyProps) {
  // The sign comes from `direction`, never from the number.
  //
  // Amounts arrive from the API always positive — direction is what makes a
  // transaction money in or money out — so asking `formatMoney` for a signed
  // rendering gave every row a "+", and a ₹757 Zomato payment read as income.
  // `formatMoney` still handles genuinely signed values (a month-on-month
  // delta, a reconciliation gap); it is only transaction amounts whose sign
  // lives in a separate field.
  const directional = signed && direction !== undefined;
  const text = formatMoney(value, {
    signed: signed && !directional,
    compactPaise,
  });
  const sign = directional ? (direction === "debit" ? "−" : "+") : "";

  return (
    <span
      data-slot="amount"
      className={cn(
        "whitespace-nowrap",
        emphasis === "display" && "text-2xl font-semibold tracking-tight",
        emphasis === "strong" && "font-semibold",
        direction === "debit" && "text-debit",
        direction === "credit" && "text-credit",
        className,
      )}
    >
      {sign}
      {direction === "credit" && !signed ? "+" : null}
      {text}
    </span>
  );
}
