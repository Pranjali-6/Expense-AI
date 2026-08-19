"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Landmark } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Money } from "@/components/shared/money";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { accounts } from "@/lib/api";
import { formatDate } from "@/lib/format";

const TYPE_LABEL: Record<string, string> = {
  savings: "Savings",
  current: "Current",
  credit_card: "Credit card",
  wallet: "Wallet",
  loan: "Loan",
};

export default function AccountsPage() {
  const { data, isLoading } = useQuery({ queryKey: ["accounts"], queryFn: accounts.list });

  return (
    <>
      <PageHeader
        title="Accounts"
        description="Discovered from your statements. No full account number is stored anywhere."
      />

      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : !data?.length ? (
        <EmptyState
          icon={Landmark}
          title="No accounts yet"
          description="Upload a statement and the account it belongs to is created automatically from the bank and the last four digits."
          action={
            <Button variant="primary" asChild>
              <Link href="/upload">Upload a statement</Link>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {data.map((account) => (
            <Card key={account.id}>
              <CardContent className="p-5">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h3 className="font-semibold">
                      {account.bank_name ?? account.bank_code}
                      <span className="ml-1.5 font-normal text-muted">
                        ••••{account.account_last4}
                      </span>
                    </h3>
                    <p className="text-xs text-muted">
                      {TYPE_LABEL[account.account_type] ?? account.account_type}
                    </p>
                  </div>
                  <Badge variant={account.status === "active" ? "success" : "neutral"}>
                    {account.status}
                  </Badge>
                </div>

                <div className="mt-4">
                  {account.current_balance !== null ? (
                    <>
                      <Money value={account.current_balance} emphasis="display" />
                      <p className="mt-0.5 text-xs text-muted">
                        {/* Extracted from a statement, never computed from the
                            ledger — so it is only as current as the last import. */}
                        As printed on the statement covering{" "}
                        {formatDate(account.balance_as_of)}
                      </p>
                    </>
                  ) : (
                    <p className="text-sm text-muted">No balance read yet</p>
                  )}
                </div>

                <dl className="mt-4 grid grid-cols-3 gap-3 border-t border-border pt-3 text-xs">
                  <div>
                    <dt className="text-muted">Transactions</dt>
                    <dd data-slot="amount" className="mt-0.5 font-medium">
                      {account.transaction_count}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted">Statements</dt>
                    <dd data-slot="amount" className="mt-0.5 font-medium">
                      {account.statement_count}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted">Covers</dt>
                    <dd className="mt-0.5 font-medium">
                      {account.coverage_start
                        ? `${formatDate(account.coverage_start)} – ${formatDate(account.coverage_end)}`
                        : "—"}
                    </dd>
                  </div>
                </dl>

                <Button variant="secondary" size="sm" className="mt-4" asChild>
                  <Link href={`/transactions?account_id=${account.id}`}>
                    View transactions
                  </Link>
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
