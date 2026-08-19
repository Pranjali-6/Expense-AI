"use client";

import * as React from "react";
import {
  AlertTriangle,
  CheckCircle2,
  FileText,
  FileUp,
  Loader2,
  Lock,
  ShieldAlert,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, statements, streamJobEvents, type JobEvent, type UploadFileResult } from "@/lib/api";
import { cn } from "@/lib/utils";

type Tracked = {
  result: UploadFileResult;
  latest?: JobEvent;
  finished: boolean;
};

/** Stages shown in order. Mirrors the pipeline rather than a timer, so the bar
 *  reflects work done rather than time passed. */
const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting for a worker",
  validating: "Checking the file",
  reading_pages: "Reading pages",
  extracting: "Extracting transactions",
  categorizing: "Categorising",
  finished: "Done",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function UploadDropzone() {
  const [dragging, setDragging] = React.useState(false);
  const [pending, setPending] = React.useState<File[]>([]);
  const [uploading, setUploading] = React.useState(false);
  const [tracked, setTracked] = React.useState<Tracked[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const cleanups = React.useRef<(() => void)[]>([]);

  React.useEffect(() => {
    // Abort every open stream on unmount, or navigating away leaves the
    // connections hanging until the server times them out.
    return () => cleanups.current.forEach((abort) => abort());
  }, []);

  function addFiles(incoming: FileList | null) {
    if (!incoming) return;
    const pdfs = Array.from(incoming).filter(
      (file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"),
    );
    setPending((current) => [...current, ...pdfs]);
    setError(pdfs.length < (incoming?.length ?? 0) ? "Only PDF files can be uploaded." : null);
  }

  /** Attach an SSE stream to one job and fold its events into `tracked`. */
  const watch = React.useCallback((jobId: string) => {
    const abort = streamJobEvents(
      jobId,
      (event) =>
        setTracked((current) =>
          current.map((entry) =>
            entry.result.job_id === jobId ? { ...entry, latest: event } : entry,
          ),
        ),
      () =>
        setTracked((current) =>
          current.map((entry) =>
            entry.result.job_id === jobId ? { ...entry, finished: true } : entry,
          ),
        ),
    );
    cleanups.current.push(abort);
  }, []);

  /** A parked statement just opened: it now has a job, so track it like any
   *  other upload rather than making the user find the file again. */
  const onUnlocked = React.useCallback(
    (statementId: string, jobId: string, pageCount: number) => {
      setTracked((current) =>
        current.map((entry) =>
          entry.result.statement_id === statementId
            ? {
                ...entry,
                finished: false,
                result: {
                  ...entry.result,
                  accepted: true,
                  locked: false,
                  job_id: jobId,
                  page_count: pageCount,
                  error_code: null,
                  message: null,
                },
              }
            : entry,
        ),
      );
      watch(jobId);
    },
    [watch],
  );

  async function submit() {
    if (!pending.length) return;
    setUploading(true);
    setError(null);

    try {
      const response = await statements.upload(pending);
      const next: Tracked[] = response.results.map((result) => ({
        result,
        // A locked file is not finished — it is waiting on the user, and its
        // row stays open with a password prompt in it.
        finished: !result.accepted && !result.locked,
      }));
      setTracked((current) => [...next, ...current]);
      setPending([]);

      for (const item of next) {
        if (item.result.job_id) watch(item.result.job_id);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed. Please try again.");
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-6">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
        className={cn(
          "flex flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-14 text-center transition-colors",
          dragging
            ? "border-primary bg-primary-subtle/40"
            : "border-border bg-surface",
        )}
      >
        <span className="grid size-12 place-items-center rounded-full bg-primary-subtle text-primary-text">
          <FileUp className="size-5" aria-hidden="true" />
        </span>
        <h2 className="mt-4 text-sm font-semibold">
          {dragging ? "Drop them here" : "Drag statement PDFs here"}
        </h2>
        <p className="mt-1 max-w-md text-sm text-muted">
          Multiple files at once, from any of your banks. Nothing is parsed in
          your browser &mdash; files are validated, encrypted and queued.
        </p>

        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          multiple
          className="sr-only"
          onChange={(e) => addFiles(e.target.files)}
        />
        <Button
          variant="secondary"
          className="mt-5"
          onClick={() => inputRef.current?.click()}
        >
          Choose files
        </Button>
      </div>

      {error && (
        <p
          role="alert"
          className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning-subtle px-3 py-2.5 text-sm text-warning-text"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {error}
        </p>
      )}

      {pending.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <div className="mb-3 flex items-center justify-between">
              <p className="text-sm font-medium">
                {pending.length} file{pending.length > 1 ? "s" : ""} ready
              </p>
              <Button
                variant="primary"
                size="sm"
                onClick={submit}
                disabled={uploading}
              >
                {uploading && <Loader2 className="size-4 animate-spin" />}
                {uploading ? "Uploading…" : "Upload"}
              </Button>
            </div>
            <ul className="divide-y divide-border">
              {pending.map((file, index) => (
                <li key={`${file.name}-${index}`} className="flex items-center gap-3 py-2">
                  <FileText className="size-4 shrink-0 text-subtle" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate text-sm">{file.name}</span>
                  <span className="shrink-0 text-xs text-muted">
                    {formatSize(file.size)}
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    onClick={() =>
                      setPending((current) => current.filter((_, i) => i !== index))
                    }
                    className="shrink-0 rounded p-1 text-subtle hover:bg-surface-sunken hover:text-foreground"
                  >
                    <X className="size-3.5" />
                  </button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {tracked.length > 0 && (
        <section aria-label="Processing" className="space-y-3">
          <h2 className="text-sm font-semibold">Processing</h2>
          {tracked.map((entry) => (
            <ProgressRow
              key={entry.result.filename + (entry.result.statement_id ?? "")}
              entry={entry}
              onUnlocked={onUnlocked}
            />
          ))}
        </section>
      )}
    </div>
  );
}

function ProgressRow({
  entry,
  onUnlocked,
}: {
  entry: Tracked;
  onUnlocked: (statementId: string, jobId: string, pageCount: number) => void;
}) {
  const { result, latest, finished } = entry;

  // Password protected, and already stored. Prompting here is the whole point:
  // the file is safe on the server, so unlocking it must not mean finding and
  // uploading it a second time.
  if (result.locked && result.statement_id) {
    return <LockedRow result={result} onUnlocked={onUnlocked} />;
  }

  // A rejected file never had a job; it shows why it was refused instead of a
  // progress bar that would never move.
  if (!result.accepted) {
    return (
      <Card>
        <CardContent className="flex items-start gap-3 p-4">
          <ShieldAlert className="mt-0.5 size-4 shrink-0 text-error-text" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">{result.filename}</p>
            <p className="mt-0.5 text-sm text-error-text">{result.message}</p>
          </div>
          <Badge variant="error">Rejected</Badge>
        </CardContent>
      </Card>
    );
  }

  const progress = latest?.progress ?? 0;
  const failed = latest?.state === "failed";
  const stage = latest?.stage ?? "queued";

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center gap-3">
          {failed ? (
            <AlertTriangle className="size-4 shrink-0 text-error-text" aria-hidden="true" />
          ) : finished ? (
            <CheckCircle2 className="size-4 shrink-0 text-success-text" aria-hidden="true" />
          ) : (
            <Loader2 className="size-4 shrink-0 animate-spin text-primary-text" aria-hidden="true" />
          )}
          <p className="min-w-0 flex-1 truncate text-sm font-medium">{result.filename}</p>
          <span data-slot="amount" className="shrink-0 text-xs text-muted">
            {progress}%
          </span>
        </div>

        <div
          className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-sunken"
          role="progressbar"
          aria-valuenow={progress}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Processing ${result.filename}`}
        >
          <div
            className={cn(
              "h-full rounded-full transition-all duration-500",
              failed ? "bg-error" : finished ? "bg-success" : "bg-primary",
            )}
            style={{ width: `${Math.max(progress, 3)}%` }}
          />
        </div>

        <p className="mt-2 text-xs text-muted">
          {latest?.message ?? STAGE_LABELS[stage] ?? stage}
        </p>
      </CardContent>
    </Card>
  );
}


/** One password-protected statement, waiting on its password.

 *  Per file, because a single field on the upload form cannot serve twelve
 *  statements from four banks. */
function LockedRow({
  result,
  onUnlocked,
}: {
  result: UploadFileResult;
  onUnlocked: (statementId: string, jobId: string, pageCount: number) => void;
}) {
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState<string | null>(null);
  const [remaining, setRemaining] = React.useState<number | null>(null);
  const statementId = result.statement_id!;

  async function attempt(event: React.FormEvent) {
    event.preventDefault();
    if (!password || busy) return;

    setBusy(true);
    setMessage(null);
    try {
      const response = await statements.unlock(statementId, password);
      if (response.unlocked && response.job_id) {
        onUnlocked(statementId, response.job_id, response.page_count);
        return;
      }
      setMessage(response.message ?? "That password did not open the statement.");
      setRemaining(response.attempts_remaining);
    } catch (err) {
      setMessage(
        err instanceof ApiError ? err.message : "Could not unlock the statement.",
      );
    } finally {
      // Cleared on every outcome. The value is a bank password the user did
      // not choose, and leaving it sitting in a form field is needless
      // exposure on a shared screen.
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start gap-3">
          <Lock className="mt-0.5 size-4 shrink-0 text-warning-text" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">{result.filename}</p>
            <p className="mt-0.5 text-sm text-muted">
              Password protected. Your file is saved &mdash; enter its password
              to finish importing it.
            </p>
          </div>
          <Badge variant="warning">Locked</Badge>
        </div>

        <form onSubmit={attempt} className="mt-3 flex flex-wrap items-start gap-2">
          <label className="sr-only" htmlFor={`password-${statementId}`}>
            Password for {result.filename}
          </label>
          <Input
            id={`password-${statementId}`}
            type="password"
            autoComplete="off"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Statement password"
            className="min-w-0 flex-1 sm:max-w-xs"
            disabled={busy}
          />
          <Button type="submit" variant="primary" disabled={busy || !password}>
            {busy && <Loader2 className="size-4 animate-spin" />}
            {busy ? "Opening…" : "Unlock"}
          </Button>
        </form>

        {message && (
          <p role="alert" className="mt-2 text-sm text-error-text">
            {message}
            {remaining !== null && remaining > 0 && (
              <span className="text-muted">
                {" "}
                {remaining} attempt{remaining === 1 ? "" : "s"} left.
              </span>
            )}
          </p>
        )}

        <p className="mt-2 text-xs text-subtle">
          Most banks use a documented formula &mdash; often the first letters of
          your name with your date of birth. The password is used once to open
          the file and is never stored.
        </p>
      </CardContent>
    </Card>
  );
}
