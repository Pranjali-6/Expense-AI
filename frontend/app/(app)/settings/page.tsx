"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  KeyRound,
  Loader2,
  MonitorSmartphone,
  ShieldAlert,
  Sparkles,
  User as UserIcon,
} from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  account,
  assistant,
  auth,
  exports,
  setAccessToken,
  type ExportFormat,
} from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";

const DELETION_PHRASE = "DELETE MY DATA";

function Section({
  icon: Icon,
  title,
  children,
  tone = "normal",
}: {
  icon: typeof UserIcon;
  title: string;
  children: React.ReactNode;
  tone?: "normal" | "danger";
}) {
  return (
    <Card className={tone === "danger" ? "border-error/40" : undefined}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Icon
            className={
              tone === "danger"
                ? "size-4 text-error-text"
                : "size-4 text-primary-text"
            }
            aria-hidden="true"
          />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const queryClient = useQueryClient();

  const me = useQuery({ queryKey: ["me"], queryFn: auth.me });
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: auth.sessions });
  const capabilities = useQuery({
    queryKey: ["assistant-capabilities"],
    queryFn: assistant.capabilities,
  });

  const [format, setFormat] = React.useState<ExportFormat>("csv");
  const [confirm, setConfirm] = React.useState("");
  const [password, setPassword] = React.useState("");

  const download = useMutation({
    mutationFn: (chosen: ExportFormat) => exports.transactions(chosen),
  });

  const revoke = useMutation({
    mutationFn: auth.revokeSession,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });

  const erase = useMutation({
    mutationFn: account.erase,
    onSuccess: () => {
      // The session is already dead server-side; clearing the in-memory token
      // stops the app trying to use it on the way out.
      setAccessToken(null);
      router.replace("/login");
    },
  });

  const canErase = confirm === DELETION_PHRASE;

  return (
    <>
      <PageHeader
        title="Settings"
        description="Your account, your data, and how to take it with you or remove it."
      />

      <div className="grid gap-6 lg:grid-cols-2">
        <Section icon={UserIcon} title="Account">
          {me.isLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : (
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <dt className="text-muted">Name</dt>
                <dd>{me.data?.full_name}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-muted">Email</dt>
                <dd className="truncate">{me.data?.email}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-muted">Sign-in</dt>
                <dd>{me.data?.auth_provider === "google" ? "Google" : "Password"}</dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-muted">Member since</dt>
                <dd>{me.data && formatDate(me.data.created_at)}</dd>
              </div>
            </dl>
          )}
        </Section>

        <Section icon={Sparkles} title="AI enrichment">
          <div className="space-y-3 text-sm">
            <p className="flex items-center gap-2">
              Status
              <Badge variant={capabilities.data?.ai_enabled ? "primary" : "neutral"}>
                {capabilities.data?.ai_enabled ? "Enabled" : "Disabled"}
              </Badge>
            </p>
            <p className="text-muted">
              {capabilities.data?.ai_enabled
                ? "A model may suggest categories and phrase answers. It never sees a raw statement, an account number or an exact amount, and every figure it quotes is checked against a computed result before you see it."
                : "No API key is configured. Every figure, category and answer is produced deterministically — the product is complete in this state, which is why it is the default."}
            </p>
            <p className="text-muted">
              Set <code className="rounded bg-surface-sunken px-1 py-0.5 text-xs">AI_ENABLED</code>{" "}
              and <code className="rounded bg-surface-sunken px-1 py-0.5 text-xs">GEMINI_API_KEY</code>{" "}
              in your <code className="rounded bg-surface-sunken px-1 py-0.5 text-xs">.env</code> to change this.{" "}
              <Link href="/privacy" className="underline">
                Privacy Center
              </Link>{" "}
              shows exactly what has been sent.
            </p>
          </div>
        </Section>

        <Section icon={Download} title="Export your data">
          <div className="space-y-3 text-sm">
            <p className="text-muted">
              Every transaction in your ledger, as you see it: effective values,
              category names, account masks. Generated per request and streamed
              straight to you — no copy is stored anywhere.
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <label className="text-xs text-muted">
                Format
                <select
                  className="mt-1 block h-9 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
                  value={format}
                  onChange={(event) => setFormat(event.target.value as ExportFormat)}
                >
                  <option value="csv">CSV — for a spreadsheet</option>
                  <option value="json">JSON — for another tool</option>
                  <option value="pdf">PDF — for reading</option>
                </select>
              </label>
              <Button
                variant="primary"
                disabled={download.isPending}
                onClick={() => download.mutate(format)}
              >
                {download.isPending ? (
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                ) : (
                  <Download className="size-4" aria-hidden="true" />
                )}
                Download
              </Button>
            </div>
            {download.isError && (
              <p className="text-error-text">{(download.error as Error).message}</p>
            )}
          </div>
        </Section>

        <Section icon={MonitorSmartphone} title="Active sessions">
          {sessions.isLoading ? (
            <Skeleton className="h-20 w-full" />
          ) : (
            <ul className="divide-y divide-border text-sm">
              {(sessions.data ?? []).map((item) => (
                <li key={item.id} className="flex items-center gap-3 py-2">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">
                      {item.user_agent ?? "Unknown device"}
                    </span>
                    <span className="block text-xs text-muted">
                      Signed in {formatDateTime(item.issued_at)}
                    </span>
                  </span>
                  {item.current ? (
                    <Badge variant="success">This device</Badge>
                  ) : (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => revoke.mutate(item.id)}
                      disabled={revoke.isPending}
                    >
                      <KeyRound className="size-4" />
                      Revoke
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>

      <div className="mt-6">
        <Section icon={ShieldAlert} title="Delete everything" tone="danger">
          <div className="space-y-3 text-sm">
            <p>
              This removes your account, every statement PDF, every transaction,
              every rule and the audit trail itself.{" "}
              <span className="font-medium">It cannot be undone</span>, and there
              is no copy anywhere to restore from.
            </p>
            <p className="text-muted">
              Export your data first if you want to keep it.
            </p>

            <div className="flex flex-wrap items-end gap-2">
              {me.data?.auth_provider !== "google" && (
                <label className="text-xs text-muted">
                  Your password
                  <Input
                    type="password"
                    className="mt-1"
                    value={password}
                    autoComplete="current-password"
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </label>
              )}
              <label className="text-xs text-muted">
                Type <span className="font-mono text-foreground">{DELETION_PHRASE}</span>
                <Input
                  className="mt-1"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  placeholder={DELETION_PHRASE}
                />
              </label>
              <Button
                variant="danger"
                disabled={!canErase || erase.isPending}
                onClick={() =>
                  erase.mutate({ password: password || undefined, confirm })
                }
              >
                {erase.isPending && (
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                )}
                Delete my account
              </Button>
            </div>

            {erase.isError && (
              <p className="text-error-text">{(erase.error as Error).message}</p>
            )}
          </div>
        </Section>
      </div>
    </>
  );
}
