"use client";

/**
 * Asking a question.
 *
 * One component behind both the Assistant screen and the dashboard query box,
 * because two implementations of "ask and render" would eventually answer the
 * same question two ways.
 *
 * The part worth defending is the label above every answer. A sentence written
 * by the server from the ledger and a sentence written by a language model
 * read identically, and the difference matters to anyone deciding how much to
 * trust it — so the source is stated, and stated in the answer's own frame
 * rather than hidden in a tooltip. When a model's wording is discarded for
 * quoting an untraceable figure, the note saying so is shown too. A system
 * that quietly swallowed that would be claiming a reliability it does not have.
 */

import * as React from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CornerDownLeft, Info, Loader2, Sparkles } from "lucide-react";

import { AnswerCardView } from "@/components/assistant/answer-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { assistant, type AssistantAnswer } from "@/lib/api";

const SOURCE_LABEL: Record<AssistantAnswer["source"], { text: string; variant: "primary" | "neutral" | "warning" }> = {
  deterministic: { text: "Computed", variant: "neutral" },
  model: { text: "Phrased by AI", variant: "primary" },
  unavailable: { text: "Not understood", variant: "warning" },
};

export function QueryBox({
  variant = "full",
  placeholder = "Ask about a category, a merchant, a month or an amount",
}: {
  variant?: "full" | "compact";
  placeholder?: string;
}) {
  const [question, setQuestion] = React.useState("");
  const [answer, setAnswer] = React.useState<AssistantAnswer | null>(null);

  const capabilities = useQuery({
    queryKey: ["assistant-capabilities"],
    queryFn: assistant.capabilities,
  });

  const ask = useMutation({
    mutationFn: assistant.ask,
    onSuccess: setAnswer,
  });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!question.trim() || ask.isPending) return;
    ask.mutate({ question });
  };

  const suggestions = capabilities.data?.suggestions ?? [];
  const shown = variant === "compact" ? suggestions.slice(0, 4) : suggestions;

  return (
    <div className="space-y-4">
      <form onSubmit={submit} className="flex gap-2">
        <div className="relative flex-1">
          <Sparkles
            className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted"
            aria-hidden="true"
          />
          <Input
            className="pl-8"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder={placeholder}
            aria-label="Ask a question about your money"
            maxLength={400}
          />
        </div>
        <Button type="submit" variant="primary" disabled={ask.isPending || !question.trim()}>
          {ask.isPending ? (
            <Loader2 className="size-4 animate-spin" aria-hidden="true" />
          ) : (
            <CornerDownLeft className="size-4" aria-hidden="true" />
          )}
          Ask
        </Button>
      </form>

      {/* One tap, no typing, no model — each of these runs the tool it names. */}
      <div className="flex flex-wrap gap-2">
        {shown.map((suggestion) => (
          <button
            key={suggestion.id}
            type="button"
            disabled={ask.isPending}
            onClick={() => {
              setQuestion(suggestion.question);
              ask.mutate({ suggestion_id: suggestion.id });
            }}
            className="rounded-full border border-border bg-surface px-3 py-1.5 text-xs text-muted transition-colors hover:border-border-strong hover:text-foreground disabled:opacity-50"
          >
            {suggestion.question}
          </button>
        ))}
      </div>

      {ask.isError && (
        <p className="text-sm text-error-text">
          {(ask.error as Error)?.message ?? "That question could not be answered."}
        </p>
      )}

      {answer && (
        <section
          aria-live="polite"
          className="rounded-lg border border-border bg-surface p-4"
        >
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <Badge variant={SOURCE_LABEL[answer.source].variant}>
              {SOURCE_LABEL[answer.source].text}
            </Badge>
            {answer.tool_calls > 0 && (
              <span className="text-xs text-muted">
                {answer.tool_calls === 1
                  ? "1 function called"
                  : `${answer.tool_calls} functions called`}
              </span>
            )}
          </div>

          <p className="text-sm leading-relaxed">{answer.answer}</p>

          {answer.notes.map((note) => (
            <p
              key={note}
              className="mt-3 flex items-start gap-2 rounded-md bg-surface-sunken px-3 py-2 text-xs text-muted"
            >
              <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              {note}
            </p>
          ))}

          {answer.cards.length > 0 && (
            <div className="mt-4 space-y-3">
              {answer.cards.map((card, index) => (
                <AnswerCardView key={`${card.tool}-${index}`} card={card} />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
