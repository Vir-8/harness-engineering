import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { AutoRefresh } from "@/components/auto-refresh";
import { Empty, LedgerTag, PageTitle, ROW_LINK, Skeleton, Stat, StatGrid, TableScroll, Td, Th, Unavailable } from "@/components/ui";
import { api } from "@/lib/api";
import { count, money } from "@/lib/format";

export const metadata: Metadata = { title: "Ledger" };

export default function LedgerPage() {
  return (
    <>
      <PageTitle lede="Our approved and paid refunds next to the payment provider's payouts, matched by reference. Anything that does not line up is listed as an exception.">Ledger</PageTitle>
      <Suspense fallback={<Skeleton rows={6} />}>
        <Reconciliation />
      </Suspense>
    </>
  );
}

async function Reconciliation() {
  let ledger;
  try {
    ledger = await api.ledger();
  } catch {
    return <Unavailable />;
  }
  if (!ledger.available) return <Unavailable>The payment provider could not be reached, so there is nothing to reconcile against.</Unavailable>;

  const { lines } = ledger;
  const matched = lines.filter((l) => l.state === "matched");
  const exceptions = lines.filter((l) => !l.ok);
  const paidOut = matched.reduce((sum, l) => sum + l.provider_amount_cents, 0);

  return (
    <div className="space-y-12">
      <AutoRefresh busy={lines.some((l) => l.state === "awaiting_payout" || l.state === "paid_not_recorded")} />
      <StatGrid>
        <Stat label="Payouts reconciled" value={count(matched.length)} sub={money(paidOut)} />
        <Stat label="Awaiting payout" value={count(lines.filter((l) => l.state === "awaiting_payout").length)} />
        <Stat label="Exceptions" value={count(exceptions.length)} tone={exceptions.length ? "accent" : undefined} />
        <Stat
          label="Paid twice"
          value={count(lines.filter((l) => l.state === "duplicate_payout").length)}
          tone={lines.some((l) => l.state === "duplicate_payout") ? "accent" : undefined}
        />
      </StatGrid>

      {lines.length === 0 ? (
        <Empty>No approved or paid requests yet.</Empty>
      ) : (
        <TableScroll>
        <table className="w-full text-[13px]">
          <thead className="border-b border-line">
            <tr>
              {/* Our side, then theirs: the two amounts sit together so a mismatch is easy to see. */}
              <Th>Request</Th>
              <Th className="hidden md:table-cell">Idempotency key</Th>
              <Th align="right">We approved</Th>
              <Th align="right">Provider paid</Th>
              <Th className="hidden pl-8 md:table-cell">Provider&apos;s payout ID</Th>
              <Th className="pl-6">Result</Th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line) => (
              <tr key={line.reference} className={`relative border-b border-line transition-colors ${line.ok ? "hover:bg-surface" : "bg-accent/[0.04]"}`}>
                <Td>
                  {line.refund_id ? (
                    <Link href={`/approvals?status=all&id=${line.refund_id}`} className={`num text-ink-2 ${ROW_LINK}`}>
                      #{line.refund_id}
                    </Link>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </Td>
                <Td className="hidden md:table-cell num text-ink-2">{line.reference}</Td>
                <Td align="right" className="num">{money(line.amount_cents)}</Td>
                <Td align="right" className="num">{line.provider_refund_ids.length ? money(line.provider_amount_cents) : "—"}</Td>
                <Td className="hidden pl-8 md:table-cell num text-ink-2">{line.provider_refund_ids.join(", ") || "—"}</Td>
                <Td className="pl-6"><LedgerTag line={line} /></Td>
              </tr>
            ))}
          </tbody>
        </table>
        </TableScroll>
      )}
    </div>
  );
}
