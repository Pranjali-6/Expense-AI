"use client";

import { useQuery } from "@tanstack/react-query";
import { Lock, ShieldCheck, Wrench } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { QueryBox } from "@/components/assistant/query-box";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { assistant } from "@/lib/api";

export default function AssistantPage() {
  const capabilities = useQuery({
    queryKey: ["assistant-capabilities"],
    queryFn: assistant.capabilities,
  });

  const aiEnabled = capabilities.data?.ai_enabled ?? false;

  return (
    <>
      <PageHeader
        title="Assistant"
        description="Ask questions about your own money. Read-only, and it cannot reach anything it was not given."
      />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <QueryBox />
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Wrench className="size-4 text-primary-text" aria-hidden="true" />
                What it is allowed to do
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <p className="text-muted">
                {capabilities.data?.max_tool_calls ?? 5} calls at most, to these
                functions and no others:
              </p>
              <ul className="space-y-1.5">
                {(capabilities.data?.tools ?? []).map((tool) => (
                  <li key={tool.name}>
                    <code className="rounded bg-surface-sunken px-1.5 py-0.5 text-xs">
                      {tool.name}()
                    </code>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Lock className="size-4 text-primary-text" aria-hidden="true" />
                Where the answer comes from
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm text-muted">
              <p>
                <span className="font-medium text-foreground">
                  Your identity is attached server-side.
                </span>{" "}
                It is not an argument any function accepts, so the assistant
                cannot ask about anyone else&rsquo;s money even if instructed
                to. There is no SQL access, no filesystem access and no
                object-storage access.
              </p>
              <p>
                <span className="font-medium text-foreground">
                  The numbers are computed before any model sees them.
                </span>{" "}
                Every figure comes from the Financial Intelligence Engine.
                Nothing is added, compared or projected by a model, and an
                answer quoting a figure that came from no function is discarded
                rather than shown to you.
              </p>
              <p>
                <span className="font-medium text-foreground">
                  Payee names are filtered.
                </span>{" "}
                Recognised businesses are named; a transfer to a private
                individual is not, so it reaches a model as &ldquo;an unnamed
                payee&rdquo;. You still see the real name on screen.
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldCheck className="size-4 text-primary-text" aria-hidden="true" />
                Mode
                <Badge variant={aiEnabled ? "primary" : "neutral"}>
                  {aiEnabled ? "AI phrasing on" : "AI off"}
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted">
              {aiEnabled ? (
                <p>
                  A model chooses which functions to call and words the result.
                  Every figure it quotes is checked against those results before
                  you see it.
                </p>
              ) : (
                <p>
                  No API key is configured, so questions are matched to a
                  function and answered directly from your ledger. The seven
                  questions above work exactly as they would with a key; free
                  wording is where a model would help.
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
