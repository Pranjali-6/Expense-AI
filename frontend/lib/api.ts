/**
 * API client.
 *
 * The access token is held **in memory only** — never in localStorage or a
 * readable cookie. A token in localStorage is readable by any script that ends
 * up on the page, which turns a single XSS into a stolen session; a token in
 * memory dies with the tab. The cost is that a page reload has no token, which
 * is what `bootstrap()` is for: it trades the httpOnly refresh cookie for a
 * fresh access token before the app renders.
 *
 * On a 401 the client refreshes once and retries. Concurrent 401s share a
 * single in-flight refresh, so ten parallel requests after an expiry produce
 * one refresh rather than ten — and ten rotations would look like token reuse
 * to the server and end the session.
 */

const BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/v1";

let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken() {
  return accessToken;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The CSRF cookie is deliberately script-readable; echoing it is the mechanism. */
function readCsrfCookie(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie.match(/(?:^|;\s*)expense_csrf=([^;]*)/);
  return match?.[1] ? decodeURIComponent(match[1]) : "";
}

async function parseError(response: Response): Promise<ApiError> {
  let code = "unknown";
  let message = "Something went wrong.";
  let details: Record<string, unknown> | undefined;

  try {
    const body = await response.json();
    code = body?.error?.code ?? code;
    message = body?.error?.message ?? message;
    details = body?.error?.details;
  } catch {
    // A non-JSON body (a proxy error page, say) — keep the generic message
    // rather than surfacing raw HTML.
  }

  return new ApiError(response.status, code, message, details);
}

async function refreshAccessToken(): Promise<boolean> {
  // Collapse concurrent refreshes. Two rotations of the same token is exactly
  // the pattern the server treats as theft.
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": readCsrfCookie() },
      });
      if (!response.ok) {
        accessToken = null;
        return false;
      }
      const body = await response.json();
      accessToken = body.access_token;
      return true;
    } catch {
      accessToken = null;
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  /** Internal: prevents infinite refresh recursion. */
  _retried?: boolean;
};

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, headers, _retried, ...rest } = options;

  const response = await fetch(`${BASE}${path}`, {
    ...rest,
    credentials: "include",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(headers as Record<string, string>),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (response.status === 401 && !_retried && !path.startsWith("/auth/refresh")) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return apiFetch<T>(path, { ...options, _retried: true });
    }
  }

  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;

  return (await response.json()) as T;
}

/**
 * A raw authenticated request, for responses that are not JSON.
 *
 * `apiFetch` parses the body, which is exactly wrong for a file download. This
 * keeps the one thing that matters — the bearer token and a single retry after
 * a refresh — and hands back the `Response` untouched.
 */
export async function authorizedFetch(
  path: string,
  init: RequestInit = {},
  retried = false,
): Promise<Response> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(init.headers as Record<string, string>),
    },
  });

  if (response.status === 401 && !retried) {
    if (await refreshAccessToken()) return authorizedFetch(path, init, true);
  }
  return response;
}

// --------------------------------------------------------------------------- //
// Auth
// --------------------------------------------------------------------------- //

export type User = {
  id: string;
  email: string;
  full_name: string;
  role: string;
  tenant_id: string;
  auth_provider: string;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
};

type TokenResponse = { access_token: string; expires_in: number; user: User };

export const auth = {
  async register(input: {
    email: string;
    password: string;
    full_name: string;
    workspace_name?: string;
  }): Promise<User> {
    const body = await apiFetch<TokenResponse>("/auth/register", {
      method: "POST",
      body: input,
    });
    accessToken = body.access_token;
    return body.user;
  },

  async login(email: string, password: string): Promise<User> {
    const body = await apiFetch<TokenResponse>("/auth/login", {
      method: "POST",
      body: { email, password },
    });
    accessToken = body.access_token;
    return body.user;
  },

  /**
   * Restore a session on page load using the httpOnly refresh cookie.
   * Returns null when there is no live session — the normal signed-out case,
   * not an error.
   */
  async bootstrap(): Promise<User | null> {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": readCsrfCookie() },
      });
      if (!response.ok) return null;
      const body: TokenResponse = await response.json();
      accessToken = body.access_token;
      return body.user;
    } catch {
      return null;
    }
  },

  async logout(): Promise<void> {
    try {
      await fetch(`${BASE}/auth/logout`, {
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": readCsrfCookie() },
      });
    } finally {
      accessToken = null;
    }
  },

  me(): Promise<User> {
    return apiFetch<User>("/auth/me");
  },

  sessions(): Promise<
    { id: string; issued_at: string; expires_at: string; user_agent: string | null; current: boolean }[]
  > {
    return apiFetch("/auth/sessions");
  },

  revokeSession(id: string): Promise<{ message: string }> {
    return apiFetch(`/auth/sessions/${id}`, { method: "DELETE" });
  },

  changePassword(current_password: string, new_password: string): Promise<{ message: string }> {
    return apiFetch("/auth/password", {
      method: "POST",
      body: { current_password, new_password },
    });
  },
};

// --------------------------------------------------------------------------- //
// Statements
// --------------------------------------------------------------------------- //

export type StatementSummary = {
  id: string;
  bank_code: string | null;
  bank_name: string | null;
  account_type: string | null;
  account_last4: string | null;
  document_type: string;
  status: string;
  trust_status: string;
  period_start: string | null;
  period_end: string | null;
  page_count: number | null;
  /** Rows in the trusted ledger. */
  transaction_count: number;
  /** Rows the parser read — not the same thing, and not conflated. */
  extracted_transaction_count: number;
  duplicate_count: number;
  file_size_bytes: number;
  created_at: string;
  processed_at: string | null;
  error_code: string | null;
  job_id: string | null;
  job_state: string | null;
  progress: number | null;
};

export type StatementHealth = {
  statement_id: string;
  reconciles: boolean;
  reconciliation_delta_paise: number | null;
  balance_continuous: boolean;
  first_divergent_row: number | null;
  first_divergent_page: number | null;
  pages_continuous: boolean;
  declared_transaction_count: number | null;
  extracted_transaction_count: number;
  ocr_page_count: number;
  total_page_count: number;
  checks: Record<string, { status: string; [k: string]: unknown }> | null;
  updated_at: string | null;
};

export type UploadFileResult = {
  filename: string;
  accepted: boolean;
  statement_id: string | null;
  job_id: string | null;
  page_count: number;
  error_code: string | null;
  message: string | null;
};

export type UploadResponse = {
  accepted: number;
  rejected: number;
  results: UploadFileResult[];
};

export type JobEvent = {
  job_id: string;
  state: string;
  stage: string;
  progress: number;
  message: string | null;
  occurred_at: string;
};

export const statements = {
  async upload(files: File[], password?: string): Promise<UploadResponse> {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    if (password) form.append("password", password);

    // Not apiFetch: a FormData body must not carry an explicit Content-Type,
    // because the browser has to add its own multipart boundary.
    const response = await fetch(`${BASE}/statements/upload`, {
      method: "POST",
      credentials: "include",
      headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      body: form,
    });

    if (response.status === 401) {
      const refreshed = await refreshAccessToken();
      if (refreshed) return statements.upload(files, password);
    }
    if (!response.ok) throw await parseError(response);
    return response.json();
  },

  list(): Promise<StatementSummary[]> {
    return apiFetch("/statements");
  },

  get(id: string): Promise<StatementSummary> {
    return apiFetch(`/statements/${id}`);
  },

  health(id: string): Promise<StatementHealth> {
    return apiFetch(`/statements/${id}/health`);
  },

  remove(id: string): Promise<{ message: string; transactions_removed: number }> {
    return apiFetch(`/statements/${id}`, { method: "DELETE" });
  },

  downloadUrl(id: string): Promise<{ url: string; expires_in: number }> {
    return apiFetch(`/statements/${id}/download-url`);
  },
};

/**
 * Follow a job's progress stream.
 *
 * Uses `fetch` and a ReadableStream rather than `EventSource`, which cannot
 * send an Authorization header. The alternative — accepting the token as a
 * query parameter — would write a credential into every access log and proxy
 * along the way.
 *
 * Returns an abort function.
 */
export function streamJobEvents(
  jobId: string,
  onEvent: (event: JobEvent) => void,
  onDone: () => void,
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch(`${BASE}/jobs/${jobId}/events`, {
        credentials: "include",
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        onDone();
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line. A chunk boundary can land
        // mid-frame, so the tail is kept until the next read completes it.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue; // a `: keepalive` comment
          try {
            const parsed = JSON.parse(line.slice(5).trim());
            if (parsed.type === "done") {
              onDone();
              return;
            }
            onEvent(parsed as JobEvent);
          } catch {
            // A partial frame; the next read will complete it.
          }
        }
      }
      onDone();
    } catch (err) {
      if ((err as Error).name !== "AbortError") onDone();
    }
  })();

  return () => controller.abort();
}

// --------------------------------------------------------------------------- //
// Transactions
//
// Every money value crosses as a **string**. JSON's only numeric type is a
// double, so parsing ₹1,23,456.78 as a number and formatting it back is not
// guaranteed to round-trip. Amounts stay strings until `formatMoney` renders
// them, and arithmetic on them happens on the server in Decimal.
// --------------------------------------------------------------------------- //

export type Transaction = {
  id: string;
  account_id: string;
  statement_id: string | null;
  txn_date: string;
  value_date: string | null;
  description: string;
  amount: string;
  direction: "debit" | "credit";
  merchant: string | null;
  payment_method: string;
  balance_after: string | null;
  category_slug: string | null;
  category_name: string | null;
  category_color: string | null;
  subcategory_slug: string | null;
  subcategory_name: string | null;
  category_source: string;
  movement_type: string;
  is_expense: boolean;
  transfer_group_id: string | null;
  confidence_extraction: string;
  confidence_merchant: string;
  confidence_category: string;
  confidence_validation: string;
  confidence_min: string;
  review_status: "auto_approved" | "flagged" | "review_required" | "resolved";
  is_verified: boolean;
  bank_code: string | null;
  bank_name: string | null;
  account_last4: string | null;
  statement_trust_status: string | null;
  source_page: number | null;
  source_row: number | null;
  created_at: string;
};

export type TransactionDetail = Transaction & {
  original_txn_date: string;
  original_description: string;
  original_amount: string;
  original_direction: string;
  original_merchant: string | null;
  field_confidence: Record<string, number> | null;
  category_reason: Record<string, unknown> | null;
  verified_at: string | null;
};

export type TransactionPage = {
  items: Transaction[];
  total: number;
  limit: number;
  offset: number;
};

export type TransactionFilters = {
  date_from?: string;
  date_to?: string;
  category?: string;
  account_id?: string;
  merchant?: string;
  search?: string;
  direction?: string;
  review_status?: string;
  is_expense?: boolean;
  min_amount?: string;
  max_amount?: string;
  max_confidence?: string;
  limit?: number;
  offset?: number;
};

export type Explanation = {
  transaction_id: string;
  category_slug: string | null;
  category_name: string | null;
  source: string;
  sentence: string;
  reason: Record<string, unknown>;
  confidence: {
    extraction: string;
    merchant: string;
    category: string;
    validation: string;
    minimum: string;
    weakest: string | null;
  };
  provenance: {
    statement_id: string | null;
    page: number | null;
    row: number | null;
    statement_trust_status: string | null;
  };
};

export type AuditEntry = {
  field_name: string;
  old_value: string | null;
  new_value: string | null;
  actor_kind: string;
  reason: string | null;
  changed_at: string;
  changed_by_name: string | null;
};

export type ReviewStats = {
  review_required: number;
  flagged: number;
  auto_approved: number;
  resolved: number;
  total: number;
  uncategorised: number;
};

export type Account = {
  id: string;
  bank_code: string;
  bank_name: string | null;
  account_type: string;
  status: string;
  account_last4: string;
  display_name: string | null;
  current_balance: string | null;
  balance_as_of: string | null;
  credit_limit: string | null;
  coverage_start: string | null;
  coverage_end: string | null;
  last_imported_at: string | null;
  transaction_count: number;
  statement_count: number;
  created_at: string;
};

function query(filters: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export const transactions = {
  list(filters: TransactionFilters = {}): Promise<TransactionPage> {
    return apiFetch(`/transactions${query(filters)}`);
  },

  get(id: string): Promise<TransactionDetail> {
    return apiFetch(`/transactions/${id}`);
  },

  explain(id: string): Promise<Explanation> {
    return apiFetch(`/transactions/${id}/explain`);
  },

  audit(id: string): Promise<AuditEntry[]> {
    return apiFetch(`/transactions/${id}/audit`);
  },

  correct(
    id: string,
    changes: Partial<{
      txn_date: string;
      description: string;
      amount: string;
      direction: string;
      merchant: string;
      payment_method: string;
      category_slug: string;
      subcategory_slug: string;
      verify: boolean;
    }>,
  ): Promise<TransactionDetail> {
    return apiFetch(`/transactions/${id}`, { method: "PATCH", body: changes });
  },

  bulkApprove(ids: string[]): Promise<{ approved: number }> {
    return apiFetch("/transactions/bulk-approve", {
      method: "POST",
      body: { transaction_ids: ids },
    });
  },

  applyToSimilar(id: string, category_slug: string): Promise<{ updated: number }> {
    return apiFetch(`/transactions/${id}/apply-to-similar`, {
      method: "POST",
      body: { category_slug },
    });
  },

  reviewStats(): Promise<ReviewStats> {
    return apiFetch("/transactions/review/stats");
  },
};

export const accounts = {
  list(): Promise<Account[]> {
    return apiFetch("/accounts");
  },
};

// --------------------------------------------------------------------------- //
// Privacy Center
// --------------------------------------------------------------------------- //

export type PrivacySummary = {
  ai_enabled: boolean;
  ai_configured: boolean;
  provider: string;
  model: string;
  known_providers: string[];
  implemented_providers: string[];
  /** Rendered from the Pydantic model's own fields, not a hardcoded copy. */
  allow_list: { name: string; description: string; optional: boolean }[];
  never_sent: string[];
  counters: {
    ai_calls_made: number;
    payloads_blocked: number;
    injections_quarantined: number;
    outputs_rejected: number;
    input_tokens: number;
    output_tokens: number;
  };
  spend: {
    total_inr: string;
    this_month_inr: string;
    monthly_budget_inr: string;
  };
  categorisation_by_source: { source: string; count: number }[];
};

export type PrivacyIncident = {
  id: string;
  kind: string;
  detector: string | null;
  field_name: string | null;
  provider: string | null;
  model_name: string | null;
  context: Record<string, unknown> | null;
  created_at: string;
};

export const privacy = {
  summary(): Promise<PrivacySummary> {
    return apiFetch("/privacy/summary");
  },
  incidents(limit = 50): Promise<PrivacyIncident[]> {
    return apiFetch(`/privacy/incidents?limit=${limit}`);
  },
};

// --------------------------------------------------------------------------- //
// Financial Intelligence
//
// Every figure here is computed deterministically on the server. No model is
// involved, which is why each one can be traced to a query.
// --------------------------------------------------------------------------- //

export type DataQuality = {
  transactions: number;
  from_untrusted_statements: number;
  awaiting_review: number;
  fully_trusted: boolean;
};

export type MonthlySummary = {
  month: string;
  expenses: string;
  net_expenses: string;
  income: string;
  refunds: string;
  transfers: string;
  net_cash_flow: string;
  savings_rate: string;
  transaction_count: number;
  expense_transaction_count: number;
  data_quality: DataQuality;
};

export type TrendPoint = {
  month: string;
  expenses: string;
  net_expenses: string;
  income: string;
  net_cash_flow: string;
  transaction_count: number;
};

export type CategoryTotal = {
  slug: string;
  name: string;
  color: string | null;
  total: string;
  transaction_count: number;
  share: string;
};

export type DailyPoint = { day: string; expenses: string; transaction_count: number };

export type MerchantTotal = {
  merchant: string;
  total: string;
  transaction_count: number;
  last_seen: string;
  category_slug: string | null;
  average: string;
};

export type Subscription = {
  id: string;
  merchant: string;
  cadence: string;
  cadence_stability: string;
  typical_amount: string;
  last_amount: string;
  estimated_annual_cost: string;
  first_charge_on: string;
  last_charge_on: string;
  next_expected_on: string | null;
  occurrence_count: number;
  status: string;
  category_slug: string | null;
  category_name: string | null;
  color: string | null;
};

export type AnomalyItem = {
  id: string;
  kind: string;
  merchant: string | null;
  detected_on: string;
  period_month: string | null;
  observed_value: string | null;
  baseline_value: string | null;
  deviation_score: string | null;
  reason: string;
  evidence: Record<string, unknown> | null;
  transaction_id: string | null;
  category_slug: string | null;
  category_name: string | null;
};

export type TimelineItem = {
  occurred_on: string;
  kind: string;
  title: string;
  summary: string | null;
  amount: string | null;
  transaction_id: string | null;
  statement_id: string | null;
  detail: string | null;
};

export type Forecast = {
  month: string;
  spent_so_far: string;
  days_elapsed: number;
  days_in_month: number;
  run_rate_projection: string;
  upcoming_recurring: string;
  projected_total: string;
  reliable: boolean;
};

export type NarrativeBlock = {
  text: string;
  model_name: string | null;
  generated_at: string | null;
};

export type MonthlyInsight = {
  month: string;
  summary: MonthlySummary;
  largest_category: CategoryTotal | null;
  fastest_growing_category: {
    slug: string;
    name: string;
    before: string;
    after: string;
    change: string;
    percent_change: string | null;
  } | null;
  largest_transaction: {
    id: string;
    merchant: string | null;
    amount: string;
    txn_date: string;
    category_name: string | null;
  } | null;
  top_merchants: MerchantTotal[];
  observations: { kind: string; text: string; values: Record<string, unknown> }[];
  recurring_load: { count: number; monthly_equivalent: string; annual: string } | null;
  /** AI phrasing of the stored snapshot. Null whenever AI is off — which is
   *  the default, and the state every screen is designed for. */
  narrative: NarrativeBlock | null;
};

export type BudgetProgress = {
  id: string;
  category_slug: string;
  category_name: string;
  color: string | null;
  amount: string;
  spent: string;
  remaining: string;
  share_used: string;
  projected_total: string;
  projection_reliable: boolean;
  alert_threshold: string;
  state: "on_track" | "warning" | "exceeded";
  days_elapsed: number;
  days_in_month: number;
};

export const intelligence = {
  summary(month?: string): Promise<MonthlySummary> {
    return apiFetch(`/intelligence/summary${month ? `?month=${month}` : ""}`);
  },
  trend(months = 12): Promise<TrendPoint[]> {
    return apiFetch(`/intelligence/trend?months=${months}`);
  },
  categories(month?: string): Promise<CategoryTotal[]> {
    return apiFetch(`/intelligence/categories${month ? `?month=${month}` : ""}`);
  },
  daily(month?: string): Promise<DailyPoint[]> {
    return apiFetch(`/intelligence/daily${month ? `?month=${month}` : ""}`);
  },
  topMerchants(month?: string, limit = 10): Promise<MerchantTotal[]> {
    return apiFetch(
      `/intelligence/top-merchants?limit=${limit}${month ? `&month=${month}` : ""}`,
    );
  },
  recurring(): Promise<Subscription[]> {
    return apiFetch("/intelligence/recurring");
  },
  anomalies(limit = 50): Promise<AnomalyItem[]> {
    return apiFetch(`/intelligence/anomalies?limit=${limit}`);
  },
  timeline(params: { limit?: number; include_transactions?: boolean } = {}): Promise<
    TimelineItem[]
  > {
    const query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.include_transactions === false) query.set("include_transactions", "false");
    return apiFetch(`/intelligence/timeline?${query.toString()}`);
  },
  forecast(month?: string): Promise<Forecast> {
    return apiFetch(`/intelligence/forecast${month ? `?month=${month}` : ""}`);
  },
  insights(month: string): Promise<MonthlyInsight> {
    return apiFetch(`/intelligence/insights/${month}`);
  },
};

export const budgets = {
  list(month?: string): Promise<BudgetProgress[]> {
    return apiFetch(`/budgets${month ? `?month=${month}` : ""}`);
  },
  create(input: { category_slug: string; amount: string }): Promise<{ id: string }> {
    return apiFetch("/budgets", { method: "POST", body: input });
  },
  update(id: string, input: { amount?: string; is_active?: boolean }): Promise<unknown> {
    return apiFetch(`/budgets/${id}`, { method: "PATCH", body: input });
  },
  remove(id: string): Promise<unknown> {
    return apiFetch(`/budgets/${id}`, { method: "DELETE" });
  },
};

// --------------------------------------------------------------------------- //
// Assistant
//
// `source` is rendered, never hidden. "deterministic" means the sentence was
// written by the server from the figures; "model" means a language model
// phrased those same figures and its wording passed the traceability check.
// Presenting the two identically would leave the reader unable to tell which
// they were reading, which is the one thing this design refuses to do.
// --------------------------------------------------------------------------- //

export type AnswerSource = "model" | "deterministic" | "unavailable";

/** Ledger filters that reproduce an answer on the Transactions screen. */
export type AnswerFilters = {
  date_from?: string;
  date_to?: string;
  category?: string;
  search?: string;
  min_amount?: string;
  max_amount?: string;
  direction?: string;
};

type CardBase = { tool: string; headline: string; filters: AnswerFilters | null };

export type AnswerTransaction = {
  id: string;
  txn_date: string;
  merchant: string | null;
  amount: string;
  direction: "debit" | "credit";
  category_name: string | null;
  category_color: string | null;
};

export type CategoryMovement = {
  slug: string;
  name: string;
  before: string;
  after: string;
  change: string;
  percent_change: string | null;
};

/**
 * A card is discriminated by `render`, so each branch of the renderer sees a
 * concrete shape. A single `Record<string, unknown>` would have been shorter
 * and would have made every access a cast — which is how a renderer ends up
 * silently drawing nothing when a field is renamed on the server.
 */
export type AnswerCard =
  | (CardBase & { render: "summary"; data: MonthlySummary })
  | (CardBase & {
      render: "categories";
      data: { period_label: string; total: string; categories: CategoryTotal[] };
    })
  | (CardBase & {
      render: "transactions";
      data: {
        period_label: string;
        matched: number;
        total: string;
        transactions: AnswerTransaction[];
      };
    })
  | (CardBase & {
      render: "merchants";
      data: { period_label: string; merchants: MerchantTotal[] };
    })
  | (CardBase & {
      render: "subscriptions";
      data: { count: number; annual_total: string; subscriptions: Subscription[] };
    })
  | (CardBase & {
      render: "comparison";
      data: {
        earlier_label: string;
        later_label: string;
        expense_change: string;
        left: MonthlySummary;
        right: MonthlySummary;
        categories: CategoryMovement[];
      };
    })
  | (CardBase & {
      render: "anomalies";
      data: { count: number; anomalies: AnomalyItem[] };
    });

export type AssistantAnswer = {
  question: string;
  answer: string;
  source: AnswerSource;
  cards: AnswerCard[];
  notes: string[];
  tool_calls: number;
  model_name: string | null;
};

export type AssistantSuggestion = { id: string; question: string; tool: string };

export type AssistantCapabilities = {
  ai_enabled: boolean;
  suggestions: AssistantSuggestion[];
  tools: { name: string; description: string }[];
  allowed_fields: string[];
  max_tool_calls: number;
};

export const assistant = {
  ask(input: { question?: string; suggestion_id?: string }): Promise<AssistantAnswer> {
    return apiFetch("/assistant/query", { method: "POST", body: input });
  },
  capabilities(): Promise<AssistantCapabilities> {
    return apiFetch("/assistant/suggestions");
  },
};

// --------------------------------------------------------------------------- //
// Categories, rules, notifications, audit, export, erasure
// --------------------------------------------------------------------------- //

export type CategoryInfo = {
  slug: string;
  name: string;
  color: string | null;
  icon: string | null;
  is_expense: boolean;
  is_income: boolean;
  transaction_count: number;
  total: string;
  subcategories: { slug: string; name: string }[];
};

export type CategoryRule = {
  id: string;
  merchant_pattern: string;
  match_type: "exact" | "contains";
  min_amount: string | null;
  max_amount: string | null;
  is_active: boolean;
  times_applied: number;
  last_applied_at: string | null;
  created_at: string;
  category_slug: string;
  category_name: string;
  color: string | null;
  subcategory_slug: string | null;
  subcategory_name: string | null;
};

export const categories = {
  list(): Promise<CategoryInfo[]> {
    return apiFetch("/categories");
  },
  rules(): Promise<CategoryRule[]> {
    return apiFetch("/categories/rules");
  },
  createRule(input: {
    merchant_pattern: string;
    category_slug: string;
    subcategory_slug?: string;
    match_type?: "exact" | "contains";
    min_amount?: string;
    max_amount?: string;
  }): Promise<{ id: string }> {
    return apiFetch("/categories/rules", { method: "POST", body: input });
  },
  deleteRule(id: string): Promise<{ deleted: boolean }> {
    return apiFetch(`/categories/rules/${id}`, { method: "DELETE" });
  },
};

export type Notification = {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  resource_type: string | null;
  resource_id: string | null;
  read_at: string | null;
  created_at: string;
};

export const notifications = {
  list(unreadOnly = false): Promise<{ items: Notification[]; unread: number }> {
    return apiFetch(`/notifications${unreadOnly ? "?unread_only=true" : ""}`);
  },
  markRead(id: string): Promise<{ marked_read: number }> {
    return apiFetch(`/notifications/${id}/read`, { method: "POST" });
  },
  markAllRead(): Promise<{ marked_read: number }> {
    return apiFetch("/notifications/read-all", { method: "POST" });
  },
};

export type AuditEntryRow = {
  id: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  succeeded: boolean;
  details: Record<string, unknown> | null;
  occurred_at: string;
  ip_address: string | null;
  actor_email: string | null;
};

export const auditLog = {
  list(params: { action?: string; before?: string; limit?: number } = {}): Promise<{
    items: AuditEntryRow[];
    next_before: string | null;
  }> {
    const query = new URLSearchParams();
    if (params.action) query.set("action", params.action);
    if (params.before) query.set("before", params.before);
    if (params.limit) query.set("limit", String(params.limit));
    const suffix = query.toString();
    return apiFetch(`/audit/logs${suffix ? `?${suffix}` : ""}`);
  },
};

export type ExportFormat = "csv" | "json" | "pdf";

export const exports = {
  /**
   * Downloads a file through an authenticated fetch.
   *
   * Not a plain `<a href>`: the access token lives in memory, never in a
   * cookie a link would carry, so the request has to be made by script and the
   * body turned into an object URL. The cost is holding the file in memory for
   * a moment; the benefit is that the token is never in a URL, a cookie, or
   * anywhere a link could leak it.
   */
  async transactions(
    format: ExportFormat,
    filters: Record<string, unknown> = {},
  ): Promise<void> {
    const response = await authorizedFetch(
      `/export/transactions?format=${format}`,
      { method: "POST", body: JSON.stringify(filters) },
    );
    if (!response.ok) throw await parseError(response);

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = disposition.match(/filename="([^"]+)"/);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = match?.[1] ?? `transactions.${format}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    // Revoked on the next tick: revoking synchronously races the download in
    // Safari, which reads the blob after the click handler returns.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  },
};

export const account = {
  /** Irreversible. The server requires the password and the exact phrase. */
  erase(input: { password?: string; confirm: string }): Promise<{ message: string }> {
    return apiFetch("/auth/account", { method: "DELETE", body: input });
  },
};
