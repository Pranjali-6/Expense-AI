"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertCircle, Check, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { FieldError, FieldHint, Input, Label } from "@/components/ui/input";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { cn } from "@/lib/utils";

const MIN_LENGTH = 12;

/**
 * Mirrors the server's policy so the rules are visible before submitting, not
 * discovered by rejection. The server is still the authority — this only saves
 * a round trip.
 *
 * Length over character classes, deliberately: mandatory symbol-and-digit rules
 * reliably produce `Password1!` and nothing safer.
 */
function passwordChecks(password: string, email: string) {
  const local = email.split("@")[0]?.toLowerCase() ?? "";
  return [
    { label: `At least ${MIN_LENGTH} characters`, ok: password.length >= MIN_LENGTH },
    { label: "Not a commonly used password", ok: password.length > 0 && !COMMON.has(password.toLowerCase()) },
    { label: "Enough variety of characters", ok: new Set(password).size >= 5 },
    {
      label: "Does not contain your email",
      ok: password.length > 0 && (local.length < 3 || !password.toLowerCase().includes(local)),
    },
  ];
}

const COMMON = new Set([
  "password", "password1", "password123", "123456789", "qwertyuiop",
  "letmein123", "welcome123", "admin123456", "iloveyou123", "changeme123",
  "expenseai123", "abcd1234567", "1234567890ab",
]);

export default function SignupPage() {
  const { register, user, loading } = useAuth();
  const router = useRouter();

  const [fullName, setFullName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    if (!loading && user) router.replace("/dashboard");
  }, [loading, user, router]);

  const checks = passwordChecks(password, email);
  const passwordReady = checks.every((check) => check.ok);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await register({ email, password, full_name: fullName });
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not create your account.",
      );
      setSubmitting(false);
    }
  }

  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Create your account</h1>
      <p className="mt-1 text-sm text-muted">
        Your own private workspace. Nothing is shared with anyone.
      </p>

      <form onSubmit={onSubmit} className="mt-8 space-y-4" noValidate>
        {error && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-error/30 bg-error-subtle px-3 py-2.5 text-sm text-error-text"
          >
            <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <div className="space-y-1.5">
          <Label htmlFor="full_name">Your name</Label>
          <Input
            id="full_name"
            autoComplete="name"
            required
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            placeholder="Priya Nair"
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            invalid={password.length > 0 && !passwordReady}
          />
          <ul className="space-y-1 pt-1">
            {checks.map((check) => (
              <li
                key={check.label}
                className={cn(
                  "flex items-center gap-1.5 text-xs",
                  check.ok ? "text-success-text" : "text-muted",
                )}
              >
                <Check
                  className={cn("size-3.5", check.ok ? "opacity-100" : "opacity-30")}
                  aria-hidden="true"
                />
                {check.label}
              </li>
            ))}
          </ul>
        </div>

        <Button
          type="submit"
          variant="primary"
          size="lg"
          className="w-full"
          disabled={submitting || !passwordReady}
        >
          {submitting && <Loader2 className="size-4 animate-spin" />}
          {submitting ? "Creating your workspace…" : "Create account"}
        </Button>

        <FieldHint>
          By creating an account you get a private workspace. Statements you
          upload are encrypted and never leave this system.
        </FieldHint>
      </form>

      <p className="mt-6 text-sm text-muted">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-primary-text hover:underline">
          Sign in
        </Link>
      </p>
    </>
  );
}
