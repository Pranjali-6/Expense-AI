"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Cpu,
  Eye,
  ShieldCheck,
  ShieldOff,
} from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { StatCard } from "@/components/shared/stat-card";
import { Money } from "@/components/shared/money";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { privacy } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

const SOURCE_LABEL: Record<string, string> = {
  user_rule: "Your own rules",
  verified_merchant_rule: "Merchant dictionary",
  deterministic_rule: "Deterministic rules",
  historical_pattern: "Your history",
  ai_model: "AI model",
  fallback_other: "Uncategorised",
};

const INCIDENT_LABEL: Record<string, string> = {
  pii_in_payload: "Payload blocked — personal data detected",
  injection_quarantined: "Prompt injection quarantined",
  output_pii_echo: "Response rejected — echoed personal data",
  output_schema_violation: "Response rejected — off-schema",
  budget_exceeded: "Call skipped — monthly budget reached",
};

export default function PrivacyPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["privacy-summary"],
    queryFn: privacy.summary,
  });
  const incidents = useQuery({
    queryKey: ["privacy-incidents"],
    queryFn: () => privacy.incidents(25),
  });

  if (isLoading || !data) {
    return (
      <>
        <PageHeader title="Privacy Center" description="What has and has not left this system." />
        <div className="space-y-4">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      </>
    );
  }

  const deterministic = data.categorisation_by_source
    .filter((row) => row.source !== "ai_model")
    .reduce((total, row) => total + row.count, 0);
  const total = data.categorisation_by_source.reduce((t, row) => t + row.count, 0);

  return (
    <>
      <PageHeader
        title="Privacy Center"
        description="What has and has not left this system — measured, not asserted."
      />

      {/* --- the headline: is anything leaving at all? -------------------- */}
      <Card className="mb-6">
        <CardContent className="flex flex-wrap items-center gap-4 p-5">
          {data.ai_enabled ? (
            <>
              <ShieldCheck className="size-8 shrink-0 text-primary-text" aria-hidden="true" />
              <div className="flex-1">
                <h2 className="font-semibold">AI enrichment is on</h2>
                <p className="text-sm text-muted">
                  {data.provider} · {data.model}. Only the six fields below are ever
                  sent, and only for transactions no rule could categorise.
                </p>
              </div>
            </>
          ) : (
            <>
              <ShieldOff className="size-8 shrink-0 text-success-text" aria-hidden="true" />
              <div className="flex-1">
                <h2 className="font-semibold">AI is switched off</h2>
                <p className="text-sm text-muted">
                  Nothing whatsoever leaves this system. Categorisation runs entirely on
                  the merchant dictionary and deterministic rules — the product is fully
                  functional in this state.
                </p>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          money={false}
          label="Transactions categorised locally"
          value={`${deterministic}${total ? ` / ${total}` : ""}`}
          hint="No model involved"
        />
        <StatCard
          money={false}
          label="AI calls made"
          value={data.counters.ai_calls_made}
          hint="Every one recorded individually"
        />
        <StatCard
          money={false}
          label="Payloads blocked"
          value={data.counters.payloads_blocked}
          hint="Personal data found before sending"
        />
        <StatCard
          money={false}
          label="Injections quarantined"
          value={data.counters.injections_quarantined}
          hint="Instruction-shaped merchant text"
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* --- what may be sent ------------------------------------------ */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Eye className="size-4 text-primary-text" aria-hidden="true" />
              Everything a model may see
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-3 text-xs text-muted">
              Read from the payload definition itself, so this list cannot claim a
              narrower perimeter than the code enforces. Any other field is rejected at
              construction — there is nowhere to put it.
            </p>
            <ul className="divide-y divide-border border-t border-border">
              {data.allow_list.map((field) => (
                <li key={field.name} className="py-2.5">
                  <div className="flex items-center gap-2">
                    <CheckCircle2
                      className="size-3.5 shrink-0 text-success-text"
                      aria-hidden="true"
                    />
                    <code className="text-sm font-medium">{field.name}</code>
                    {field.optional && (
                      <Badge variant="neutral" className="ml-auto">
                        optional
                      </Badge>
                    )}
                  </div>
                  <p className="ml-5.5 mt-0.5 pl-0.5 text-xs text-muted">
                    {field.description}
                  </p>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>

        {/* --- what never is --------------------------------------------- */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Ban className="size-4 text-error-text" aria-hidden="true" />
              Never sent, under any circumstances
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-3 text-xs text-muted">
              A payment to a person is never sent at all: an unrecognised name on a
              transfer rail is withheld and the transaction goes to review instead.
            </p>
            <ul className="space-y-2">
              {data.never_sent.map((item) => (
                <li key={item} className="flex items-start gap-2 text-sm">
                  <Ban
                    className="mt-0.5 size-3.5 shrink-0 text-error-text"
                    aria-hidden="true"
                  />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>

      {/* --- who decided each category --------------------------------- */}
      <Card className="mt-6">
        <CardHeader>
          <CardTitle>How your transactions were categorised</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="divide-y divide-border border-t border-border">
            {data.categorisation_by_source.map((row) => (
              <li key={row.source} className="flex items-center gap-3 py-2.5 text-sm">
                {row.source === "ai_model" ? (
                  <Cpu className="size-4 text-info-text" aria-hidden="true" />
                ) : (
                  <ShieldCheck className="size-4 text-success-text" aria-hidden="true" />
                )}
                <span className="flex-1">{SOURCE_LABEL[row.source] ?? row.source}</span>
                <span data-slot="amount" className="font-medium">
                  {row.count}
                </span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>

      {/* --- spend ------------------------------------------------------ */}
      {data.ai_enabled && (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>Spend</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">This month</dt>
                <dd className="mt-1">
                  <Money value={data.spend.this_month_inr} emphasis="strong" />
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">Budget</dt>
                <dd className="mt-1">
                  <Money value={data.spend.monthly_budget_inr} emphasis="strong" />
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-muted">Tokens</dt>
                <dd data-slot="amount" className="mt-1 font-semibold">
                  {data.counters.input_tokens + data.counters.output_tokens}
                </dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      )}

      {/* --- incidents -------------------------------------------------- */}
      <Card className="mt-6">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <AlertTriangle className="size-4 text-warning-text" aria-hidden="true" />
            Times a control fired
          </CardTitle>
        </CardHeader>
        <CardContent>
          {!incidents.data?.length ? (
            <p className="text-sm text-muted">
              Nothing yet. Each entry records which detector fired and on which field —
              never what it matched, because storing the evidence would itself be the
              leak.
            </p>
          ) : (
            <ul className="divide-y divide-border border-t border-border">
              {incidents.data.map((incident) => (
                <li key={incident.id} className="py-2.5 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">
                      {INCIDENT_LABEL[incident.kind] ?? incident.kind}
                    </span>
                    {incident.detector && (
                      <Badge variant="warning">{incident.detector}</Badge>
                    )}
                    <span className="ml-auto text-xs text-muted">
                      {formatDateTime(incident.created_at)}
                    </span>
                  </div>
                  {incident.field_name && (
                    <p className="mt-0.5 text-xs text-muted">
                      field: {incident.field_name}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Separator className="my-6" />
      <p className="text-xs text-muted">
        An AI model in this system never sees a PDF, never touches the database, never
        performs arithmetic and cannot overrule a correction you have made. It suggests
        a category for a merchant name and nothing else.
      </p>
    </>
  );
}
