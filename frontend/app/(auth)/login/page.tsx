"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertCircle, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { FieldError, Input, Label } from "@/components/ui/input";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

function LoginForm() {
  const { login, user, loading } = useAuth();
  const router = useRouter();
  const params = useSearchParams();

  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  // Already signed in — do not show a login form to someone with a session.
  React.useEffect(() => {
    if (!loading && user) router.replace(params.get("next") || "/dashboard");
  }, [loading, user, router, params]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
    } catch (err) {
      // The server deliberately returns the same message for an unknown email
      // and a wrong password, so the form is not an account-existence oracle.
      // Show what it said rather than trying to be more helpful.
      setError(
        err instanceof ApiError ? err.message : "Could not sign in. Please try again.",
      );
      setSubmitting(false);
    }
  }

  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
      <p className="mt-1 text-sm text-muted">
        Welcome back. Your ledger is where you left it.
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
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <Button
          type="submit"
          variant="primary"
          size="lg"
          className="w-full"
          disabled={submitting}
        >
          {submitting && <Loader2 className="size-4 animate-spin" />}
          {submitting ? "Signing in…" : "Sign in"}
        </Button>
      </form>

      <p className="mt-6 text-sm text-muted">
        New here?{" "}
        <Link href="/signup" className="font-medium text-primary-text hover:underline">
          Create an account
        </Link>
      </p>
    </>
  );
}


/**
 * `useSearchParams` opts a route into dynamic rendering, and Next requires a
 * Suspense boundary around it or the build fails on prerender. The fallback is
 * the form's own skeleton so the transition is invisible.
 */
export default function LoginPage() {
  return (
    <React.Suspense
      fallback={
        <div className="space-y-4">
          <div className="h-7 w-28 rounded skeleton-shimmer" />
          <div className="h-10 w-full rounded skeleton-shimmer" />
          <div className="h-10 w-full rounded skeleton-shimmer" />
          <div className="h-11 w-full rounded skeleton-shimmer" />
        </div>
      }
    >
      <LoginForm />
    </React.Suspense>
  );
}
