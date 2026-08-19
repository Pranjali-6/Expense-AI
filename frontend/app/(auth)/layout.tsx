import Link from "next/link";
import { Lock, ScanLine, ShieldCheck } from "lucide-react";

const PROMISES = [
  {
    icon: ScanLine,
    title: "Read, not guessed",
    body: "Statements are parsed deterministically. An AI model is never the extraction engine.",
  },
  {
    icon: Lock,
    title: "Your PDFs stay here",
    body: "No document, account number, card number or UPI ID is ever sent to an AI model.",
  },
  {
    icon: ShieldCheck,
    title: "Reconciled before it counts",
    body: "A statement whose arithmetic does not balance is flagged, never quietly trusted.",
  },
];

export default function AuthLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-2">
      {/* Form side */}
      <div className="flex flex-col justify-center px-6 py-12 sm:px-12">
        <div className="mx-auto w-full max-w-sm">
          <Link href="/" className="mb-8 inline-flex items-center gap-2.5">
            <span className="grid size-9 place-items-center rounded-lg bg-primary text-primary-foreground">
              <ShieldCheck className="size-4" aria-hidden="true" />
            </span>
            <span>
              <span className="block text-sm font-semibold tracking-tight">
                Expense AI
              </span>
              <span className="block text-xs text-subtle">
                Financial intelligence
              </span>
            </span>
          </Link>

          {children}
        </div>
      </div>

      {/* Brand side — hidden on small screens, where it would just push the
          form below the fold. */}
      <div className="relative hidden flex-col justify-center border-l border-border bg-surface px-12 lg:flex">
        <div className="max-w-md">
          <h2 className="text-2xl font-semibold tracking-tight">
            Drop in your statements.
            <span className="block text-primary-text">Never type a transaction.</span>
          </h2>
          <p className="mt-3 text-sm leading-relaxed text-muted">
            Upload bank and credit-card statement PDFs from HDFC, ICICI, SBI,
            Axis and more. Every transaction is extracted, validated and
            reconciled before it reaches your ledger.
          </p>

          <ul className="mt-10 space-y-6">
            {PROMISES.map((promise) => (
              <li key={promise.title} className="flex gap-3.5">
                <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg bg-primary-subtle text-primary-text">
                  <promise.icon className="size-4" aria-hidden="true" />
                </span>
                <span>
                  <span className="block text-sm font-medium">{promise.title}</span>
                  <span className="mt-0.5 block text-sm text-muted">
                    {promise.body}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
