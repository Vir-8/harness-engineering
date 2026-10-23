import type { Metadata } from "next";
import Link from "next/link";
import { connection } from "next/server";
import { Suspense } from "react";

import { AutoRefresh } from "@/components/auto-refresh";
import { RequestDetail } from "@/components/request-detail";
import { Empty, PageTitle, ROW_LINK, Skeleton, StatusTag, TableScroll, Td, Th, Unavailable } from "@/components/ui";
import { api } from "@/lib/api";
import { ago, money } from "@/lib/format";
import { hasLinkedAccounts, type RefundStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Approvals" };

const TABS = [
  { key: "pending", label: "Awaiting approval" },
  { key: "approved", label: "Approved" },
  { key: "issued", label: "Paid" },
  { key: "rejected", label: "Rejected" },
  { key: "all", label: "All" },
] as const;

type Tab = (typeof TABS)[number]["key"];

function asTab(value: unknown): Tab {
  return TABS.some((t) => t.key === value) ? (value as Tab) : "pending";
}

type SearchParams = PageProps<"/approvals">["searchParams"];

// From lg up the list and the request panel sit side by side. The title takes the first row and the list the second;
// a request's panel spans both and is centred in a box exactly as tall as the screen leaves above the footer, so a
// short page does not scroll into empty space. An empty tab's note instead starts level with the tabs.
// Below lg there is no room beside the list: a selected request opens as a full-screen sheet over it instead.
const PANEL = "lg:sticky lg:top-20 lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:flex lg:h-[calc(100dvh-13rem)] lg:items-center lg:self-start";
const NOTE = "hidden lg:col-start-2 lg:row-start-2 lg:block lg:self-start";

export default function ApprovalsPage({ searchParams }: PageProps<"/approvals">) {
  return (
    // -mb-14 trims the page's bottom padding here, which the panel's box already accounts for
    <div className="grid grid-cols-1 gap-x-10 lg:-mb-14 lg:grid-cols-[minmax(0,1fr)_360px] lg:grid-rows-[auto_1fr] lg:gap-x-8 xl:grid-cols-[minmax(0,1fr)_400px] xl:gap-x-10">
      <div className="lg:col-start-1 lg:row-start-1">
        <PageTitle lede="Refund requests filed by the agent wait here for a reviewer. Approving one queues its payout. The notes on linked accounts come from the Neo4j graph and are not shown to the agent.">
          Approvals
        </PageTitle>
      </div>
      <div className="lg:col-start-1 lg:row-start-2">
        <Suspense fallback={<Skeleton rows={6} />}>
          <Requests searchParams={searchParams} />
        </Suspense>
      </div>
      <Suspense
        fallback={
          <aside className={PANEL}>
            <div className="pulse hidden h-64 w-full border border-line bg-surface/60 lg:block" />
          </aside>
        }
      >
        <Selected searchParams={searchParams} />
      </Suspense>
    </div>
  );
}

async function Selected({ searchParams }: { searchParams: SearchParams }) {
  await connection();
  const params = await searchParams;
  const tab = asTab(params.status);
  const selected = Number(params.id) || null;
  if (!selected) {
    // Nothing chosen: beside the list (lg) show the first request rather than an empty box. On a phone the list stays
    // as it is: opening the sheet unasked would hide it.
    const first = (await listFor(tab).catch(() => null))?.[0];
    if (!first) {
      return (
        <aside className={NOTE}>
          <NothingHere tab={tab} />
        </aside>
      );
    }
    return (
      <aside className={PANEL}>
        <div className="hidden w-full lg:flex lg:max-h-full lg:flex-col">
          <RequestDetail id={first.refund_id} />
        </div>
      </aside>
    );
  }
  return (
    <aside className={PANEL}>
      <div className="fixed inset-0 z-30 overflow-y-auto bg-paper px-4 pb-6 pt-4 sm:px-6 lg:static lg:z-auto lg:flex lg:max-h-full lg:w-full lg:flex-col lg:overflow-visible lg:bg-transparent lg:p-0">
        <Link href={`/approvals?status=${tab}`} scroll={false} className="mb-4 inline-block text-[13px] text-muted hover:text-ink lg:hidden">
          ← Back to requests
        </Link>
        <RequestDetail id={selected} />
      </div>
    </aside>
  );
}

function listFor(tab: Tab) {
  return api.refunds(tab === "all" ? undefined : (tab as RefundStatus));
}

const EMPTY: Record<Tab, { title: string; text: string }> = {
  pending: {
    title: "Nothing waiting",
    text: "Requests appear here when the agent files one. Each comes with its order, the customer's history and any accounts linked to them by a shared card or address, for a reviewer to approve or reject.",
  },
  approved: {
    title: "Nothing being paid",
    text: "An approved request only stays here until the worker pays it, usually a few seconds. Then it moves to Paid.",
  },
  issued: { title: "Nothing paid yet", text: "Requests move here once the worker has paid them through the payment provider." },
  rejected: { title: "Nothing rejected", text: "Requests a reviewer turns down move here, with the reason they gave." },
  all: { title: "No requests yet", text: "Requests appear here when the agent files one, and stay here whatever happens to them." },
};

function NothingHere({ tab }: { tab: Tab }) {
  return (
    <div className="border border-line bg-surface px-5 py-6">
      <div className="label mb-3">{EMPTY[tab].title}</div>
      <p className="text-[13px] leading-relaxed text-ink-2">{EMPTY[tab].text}</p>
      <Link href="/agent" className="mt-4 inline-block text-[13px] text-ink hover:underline">
        Ask the agent for a refund →
      </Link>
    </div>
  );
}

async function Requests({ searchParams }: { searchParams: SearchParams }) {
  await connection();  // rendered per request: the table shows times relative to now
  const params = await searchParams;
  const tab = asTab(params.status);
  const chosen = Number(params.id) || null;

  let rows;
  try {
    rows = await listFor(tab);
  } catch {
    return <Unavailable />;
  }
  // With nothing chosen, the panel shows the first request: mark it, but only where the panel is visible (lg).
  const selected = chosen ?? rows[0]?.refund_id ?? null;

  return (
    <>
      <AutoRefresh busy={rows.some((r) => r.status === "approved")} />
      <div>
        {/* The baseline is drawn inside the row (not as its border, overlapped by the active tab), so nothing sticks out:
            the row can scroll sideways on a phone and never up and down. */}
        <div className="no-scrollbar relative mb-1 flex gap-6 overflow-x-auto overflow-y-hidden after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-line">
          {TABS.map((t) => (
            <Link
              key={t.key}
              href={`/approvals?status=${t.key}`}
              className={`relative z-10 shrink-0 border-b pb-2.5 text-[13px] transition-colors ${
                t.key === tab ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"
              }`}
            >
              {t.label}
            </Link>
          ))}
        </div>

        {rows.length === 0 ? (
          <Empty>{tab === "pending" ? "Nothing is waiting for approval." : "No requests here."}</Empty>
        ) : (
          <TableScroll>
          <table className="w-full text-[13px]">
            <thead className="border-b border-line">
              <tr>
                <Th>Request</Th>
                <Th>Customer</Th>
                <Th className="hidden md:table-cell lg:hidden xl:table-cell">Item</Th>
                <Th align="right">Amount</Th>
                <Th>Status</Th>
                <Th align="right" className="hidden sm:table-cell">Updated</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const active = r.refund_id === selected;
                const shown = active ? (chosen ? "bg-surface" : "lg:bg-surface") : "";
                const flagged = hasLinkedAccounts(r.risk_flags);
                return (
                  <tr key={r.refund_id} className={`relative border-b border-line transition-colors hover:bg-surface/70 ${shown}`}>
                    <Td>
                      {active && <span className={`absolute -left-3 top-1/2 h-3.5 w-px -translate-y-1/2 bg-ink ${chosen ? "" : "hidden lg:block"}`} />}
                      <Link href={`/approvals?status=${tab}&id=${r.refund_id}`} scroll={false} className={`num text-ink-2 ${ROW_LINK}`}>
                        #{r.refund_id}
                      </Link>
                    </Td>
                    <Td>
                      <span className="inline-flex items-center gap-2 whitespace-nowrap">
                        {r.customer_name ?? "—"}
                        {flagged && <span title="Linked-account notes" className="size-1.5 rounded-full bg-accent" />}
                      </span>
                    </Td>
                    <Td className="hidden text-ink-2 md:table-cell lg:hidden xl:table-cell">
                      {r.item} <span className="num text-faint">{r.order_id}</span>
                    </Td>
                    <Td align="right" className="num">{money(r.amount_cents)}</Td>
                    <Td><StatusTag status={r.status} /></Td>
                    <Td align="right" className="hidden whitespace-nowrap text-muted sm:table-cell">{ago(r.updated_at ?? r.created_at)}</Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </TableScroll>
        )}
      </div>
    </>
  );
}
