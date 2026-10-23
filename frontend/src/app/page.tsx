import Link from "next/link";
import { connection } from "next/server";
import { Suspense, type ReactNode } from "react";

import { AutoRefresh } from "@/components/auto-refresh";
import { Empty, LedgerTag, ROW_LINK, SectionTitle, Skeleton, Unavailable } from "@/components/ui";
import quality from "@/data/quality.json";
import { api } from "@/lib/api";
import { ago, count, money, tinyMoney, withinLastDay } from "@/lib/format";
import { sourceUrl } from "@/lib/repo";
import { hasLinkedAccounts, type ActivityItem, type LedgerLine, type RefundSummary } from "@/lib/types";

export default function OverviewPage() {
  return (
    <>
      <Hero />
      <Suspense fallback={<Skeleton rows={8} />}>
        <Overview />
      </Suspense>
    </>
  );
}

// The first thing anyone sees: what this is, the idea it is built on, and where to go to try it.
function Hero() {
  return (
    <section className="mb-12 sm:mb-14">
      <h1 className="max-w-[1080px] text-[30px] font-medium leading-[1.1] tracking-[-0.035em] sm:text-[44px] sm:leading-[1.06]">
        An agent harness for financial operations.
      </h1>
      <p className="mt-5 max-w-[560px] text-[15px] leading-relaxed text-ink-2">
        Running here on refunds for two online stores. Ask the agent for one, approve it, and watch it get paid.
      </p>
      <div className="mt-7 flex flex-wrap gap-2">
        <Link href="/agent" className="border border-ink bg-ink px-4 py-2.5 text-[13px] text-paper transition-colors hover:bg-ink-2">
          Ask the agent →
        </Link>
        <Link href="/architecture" className="border border-faint px-4 py-2.5 text-[13px] text-ink-2 transition-colors hover:border-ink hover:text-ink">
          How it works
        </Link>
      </div>
    </section>
  );
}

async function Overview() {
  await connection();  // rendered per request: the figures are relative to now
  const [ops, recent, ledger, feed] = await Promise.allSettled([api.ops(), api.refunds(), api.ledger(), api.activity()]);
  const lines = ledger.status === "fulfilled" && ledger.value.available ? ledger.value.lines : null;
  const exceptions = lines?.filter((l) => !l.ok) ?? null;
  const matched = lines?.filter((l) => l.state === "matched").length ?? 0;
  const guarantees = <Guarantees pending={ops.status === "fulfilled" ? ops.value.by_status.pending ?? null : null} exceptions={exceptions} matched={matched} />;
  if (ops.status === "rejected") {
    return (
      <div className="space-y-14">
        {guarantees}
        <Unavailable />
      </div>
    );
  }

  const { by_status, outbox_unsent, payouts_queue, dead_letters } = ops.value;
  const pending = by_status.pending ?? { count: 0, amount_cents: 0 };
  const approved = by_status.approved ?? { count: 0, amount_cents: 0 };
  const all = (recent.status === "fulfilled" ? recent.value : []).toSorted((a, b) => b.created_at.localeCompare(a.created_at));  // newest first
  const busy = approved.count > 0 || outbox_unsent > 0 || !!payouts_queue?.in_flight;

  const filedToday = all.filter((r) => withinLastDay(r.created_at));
  const waiting = all.filter((r) => r.status === "pending");
  const paidToday = all.filter((r) => r.status === "issued" && withinLastDay(paidAt(r)));
  const lastPaid = all.filter((r) => r.status === "issued").sort((a, b) => paidAt(b).localeCompare(paidAt(a)))[0];
  const queue = [
    ["Outbox", outbox_unsent],
    ["Queued", payouts_queue?.waiting],
    ["In flight", payouts_queue?.in_flight],
    ["Dead letters", dead_letters?.waiting],
  ] as const;

  return (
    <div className="space-y-14">
      <AutoRefresh busy={busy} />

      {guarantees}

      <section>
        <SectionTitle>Refund pipeline</SectionTitle>
        <ol className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Stage
            step="01"
            name="Agent"
            href="/agent"
            value={count(filedToday.length)}
            caption="filed in the last 24 h"
            about="Looks up the order and the merchant's refund policy, then files a request with submit_order_refund."
          >
            {all[0] ? <Request r={all[0]} when={all[0].created_at} label="Latest" /> : <Quiet>Nothing filed yet.</Quiet>}
          </Stage>
          <Stage
            step="02"
            name="Approval"
            href="/approvals"
            value={count(pending.count)}
            caption={`waiting · ${money(pending.amount_cents)}`}
            about="Eligibility and duplicates are checked in the database first. A reviewer approves or rejects, with notes on linked accounts."
          >
            {waiting.length ? (
              <div className="space-y-3">
                {waiting.slice(0, 2).map((r) => (
                  <Request key={r.refund_id} r={r} when={r.created_at} />
                ))}
                {waiting.length > 2 && <Quiet>Oldest waiting {ago(waiting.at(-1)!.created_at)}</Quiet>}
              </div>
            ) : (
              <Quiet>Nothing waiting.</Quiet>
            )}
          </Stage>
          <Stage
            step="03"
            name="Payout"
            href="/approvals?status=approved"
            value={count(approved.count)}
            caption="being paid now"
            about="Approval queues the payout job in the same transaction. A worker pays through the provider with an idempotency key, so retries never pay twice."
            alert={!!dead_letters?.waiting}
          >
            {queue.every(([, n]) => !n) ? (
              <Quiet>Queue empty. Approved refunds are usually paid within seconds.</Quiet>
            ) : (
              <dl className="space-y-1.5">
                {queue.map(([label, n]) => (
                  <div key={label} className={`flex items-baseline gap-2 text-[12px] ${label === "Dead letters" && n ? "text-accent" : "text-ink-2"}`}>
                    <dt>{label}</dt>
                    <span className="flex-1 translate-y-[-3px] border-b border-dotted border-line-strong" />
                    <dd className="num">{n == null ? "—" : count(n)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </Stage>
          <Stage
            step="04"
            name="Paid"
            href="/approvals?status=issued"
            value={count(paidToday.length)}
            caption="paid in the last 24 h"
            about="Marked as issued, with the provider's payout ID and an entry in the audit log."
          >
            {lastPaid ? <Request r={lastPaid} when={paidAt(lastPaid)} label="Latest" extra={lastPaid.provider_refund_id} /> : <Quiet>Nothing paid yet.</Quiet>}
          </Stage>
          <Stage
            step="05"
            name="Reconciliation"
            href="/ledger"
            value={exceptions == null ? "—" : count(exceptions.length)}
            caption={exceptions == null ? "provider unavailable" : exceptions.length ? "exceptions to review" : "exceptions"}
            about="Our refunds compared with the provider's payouts, matched by reference."
            alert={!!exceptions?.length}
          >
            {exceptions == null ? (
              <Quiet>The provider could not be reached.</Quiet>
            ) : exceptions.length ? (
              <div className="space-y-2">
                {exceptions.slice(0, 2).map((line) => (
                  <div key={line.reference} className="text-[12px]">
                    <LedgerTag line={line} />
                    <div className="num mt-0.5 text-muted">{line.reference}</div>
                  </div>
                ))}
              </div>
            ) : (
              <Quiet>All {count(matched)} payouts match the provider&apos;s ledger, checked as this page loaded.</Quiet>
            )}
          </Stage>
        </ol>
      </section>

      <section>
        <SectionTitle aside={<Link href="/quality" className="hover:text-ink">Quality →</Link>}>Agent quality</SectionTitle>
        <div className="grid grid-cols-2 gap-x-3 gap-y-6 lg:grid-cols-4">
          <Fact value={`${Math.round((quality.primary.passed / quality.primary.cases) * 100)}%`} label={`eval pass rate (${quality.primary.passed} of ${quality.primary.cases} runs)`} />
          <Fact value={`${quality.primary.latency_mean_s} s`} label="average time per question" />
          <Fact value={tinyMoney(quality.primary.cost_per_case)} label="model cost per question" />
          <Fact
            value={`${quality.primary.checks.retrieval_hit_at_1[0]} / ${quality.primary.checks.retrieval_hit_at_1[1]}`}
            label="questions where the right policy passage came first"
          />
        </div>
      </section>

      <section>
        <SectionTitle aside={<Link href="/approvals?status=all" className="hover:text-ink">All requests →</Link>}>Activity</SectionTitle>
        {feed.status === "rejected" ? <Unavailable /> : feed.value.length === 0 ? <Empty>Nothing has happened yet. Ask the agent for a refund to start.</Empty> : <Feed items={feed.value} />}
      </section>
    </div>
  );
}

function paidAt(r: RefundSummary): string {
  return r.updated_at ?? r.created_at;
}

// --- what the harness guarantees ---

// Each claim with its evidence: a live figure where there is one, otherwise the eval or the test that makes the failure
// happen. Told in the order a refund goes through them.
function Guarantees({
  pending,
  exceptions,
  matched,
}: {
  pending: { count: number; amount_cents: number } | null;
  exceptions: LedgerLine[] | null;
  matched: number;
}) {
  const [safe, safety] = quality.primary.categories.safety;
  return (
    <section className="grid grid-cols-1 gap-px border border-faint bg-line sm:grid-cols-2 lg:grid-cols-4">
      <Guarantee title="The model can't move money" proof={<Proof href="/quality">{safe} of {safety} jailbreaks refused</Proof>}>
        It can look up orders, read the policy and file requests. Eligibility is checked in code, and nothing it can call approves or pays.
      </Guarantee>
      <Guarantee
        title="A person approves every refund"
        proof={
          pending == null ? (
            <Proof>Unavailable</Proof>
          ) : (
            <Proof href="/approvals" ok={!!pending.count}>
              {pending.count ? `${count(pending.count)} waiting · ${money(pending.amount_cents)}` : "Nothing waiting"}
            </Proof>
          )
        }
      >
        The reviewer sees the customer&apos;s history and any accounts that share their card. Every decision is logged with who made it.
      </Guarantee>
      <Guarantee title="No refund is paid twice" proof={<Proof href={sourceUrl("tests/test_payout_worker.py")}>Crash-tested mid-payout</Proof>}>
        Each payout carries a key named after its refund, so a retry after a crash gets the first payout back instead of a new one.
      </Guarantee>
      <Guarantee
        title="Every payout is reconciled"
        proof={
          exceptions == null ? (
            <Proof>Provider unreachable</Proof>
          ) : exceptions.length ? (
            <Proof href="/ledger" alert>
              {count(exceptions.length)} to review
            </Proof>
          ) : (
            <Proof href="/ledger">All {count(matched)} match, checked just now</Proof>
          )
        }
      >
        Our records are matched against the provider&apos;s own ledger, so a missing, doubled or wrong payout shows up.
      </Guarantee>
    </section>
  );
}

function Guarantee({ title, proof, children }: { title: string; proof: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col bg-surface px-5 pb-5 pt-4">
      <h2 className="text-[16px] font-medium tracking-[-0.015em]">{title}</h2>
      <p className="mt-2 flex-1 text-[13px] leading-relaxed text-ink-2">{children}</p>
      <div className="mt-4">{proof}</div>
    </div>
  );
}

// The evidence under a claim: green when it holds, red when it needs a look, grey when it could not be checked.
function Proof({ href, ok = true, alert, children }: { href?: string; ok?: boolean; alert?: boolean; children: ReactNode }) {
  const dot = !href ? "bg-faint" : alert ? "bg-accent" : ok ? "bg-ok" : "bg-faint";
  const body = (
    <>
      <span className={`size-1.5 shrink-0 translate-y-[-1px] rounded-full ${dot}`} />
      {children}
      {href && <span className="text-muted transition-transform group-hover:translate-x-0.5">{href.startsWith("http") ? "↗" : "→"}</span>}
    </>
  );
  const style = `num group inline-flex items-center gap-2 text-[12px] ${alert ? "text-accent" : href ? "text-ink" : "text-muted"}`;
  if (!href) return <span className={style}>{body}</span>;
  return href.startsWith("http") ? (
    <a href={href} target="_blank" rel="noreferrer" className={style}>
      {body}
    </a>
  ) : (
    <Link href={href} className={style}>
      {body}
    </Link>
  );
}

// --- the pipeline ---

function Stage({
  step,
  name,
  href,
  value,
  caption,
  about,
  alert,
  children,
}: {
  step: string;
  name: string;
  href: string;
  value: string;
  caption: string;
  about: string;
  alert?: boolean;
  children: ReactNode;
}) {
  return (
    // In the 2-column layout the fifth card would sit alone at half width: it takes the whole row instead.
    <li className="relative sm:last:col-span-2 lg:last:col-span-1">
      {step !== "01" && <span className="absolute -left-3 top-[50px] z-10 hidden w-3 justify-center text-[14px] text-muted lg:flex">→</span>}
      <Link
        href={href}
        title={about}
        className={`group flex h-full flex-col border bg-surface px-4 pb-4 pt-3.5 transition-colors hover:border-ink ${alert ? "border-accent" : "border-faint"}`}
      >
        <span className="label !text-[12px] !text-ink">
          <span className="text-muted">{step}</span> {name}
        </span>
        <div className={`num mt-4 text-[28px] leading-none ${alert ? "text-accent" : ""}`}>{value}</div>
        <div className="num mt-2 text-[13px] text-ink-2">{caption}</div>
        <div className="mt-4 flex-1 border-t border-line pt-3">{children}</div>
      </Link>
    </li>
  );
}

function Request({ r, when, label, extra }: { r: RefundSummary; when: string; label?: string; extra?: string | null }) {
  const flagged = hasLinkedAccounts(r.risk_flags);
  return (
    <div className="text-[12px] leading-snug">
      {label && <div className="label mb-1 !text-[10px]">{label}</div>}
      <div className="flex items-center gap-1.5 text-ink">
        <span className="truncate">{r.customer_name ?? "Unknown customer"}</span>
        {flagged && <span title="Linked-account notes" className="size-1.5 shrink-0 rounded-full bg-accent" />}
      </div>
      <div className="truncate text-muted">
        {r.item} · <span className="num">{money(r.amount_cents)}</span> · {ago(when)}
      </div>
      {extra && <div className="num truncate text-faint">{extra}</div>}
    </div>
  );
}

function Quiet({ children }: { children: ReactNode }) {
  return <p className="text-[12px] leading-snug text-muted">{children}</p>;
}

// --- activity ---

const EVENT: Record<ActivityItem["event"], { label: string; dot: string }> = {
  filed: { label: "Filed", dot: "bg-warn" },
  approved: { label: "Approved", dot: "bg-ink" },
  issued: { label: "Paid", dot: "bg-ok" },
  rejected: { label: "Rejected", dot: "bg-accent" },
};

function Feed({ items }: { items: ActivityItem[] }) {
  return (
    <ol>
      {items.map((item, i) => {
        const { label, dot } = EVENT[item.event];
        return (
          // Phones: when and what on the first line with the amount, the details on a second. From sm up: one line.
          <li
            key={i}
            className="relative grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 gap-y-1 border-b border-line py-2.5 text-[13px] transition-colors hover:bg-surface sm:grid-cols-[96px_96px_minmax(0,1fr)_auto]"
          >
            <span className="flex items-baseline gap-4 sm:contents">
              <span className="text-[12px] text-muted">{ago(item.at)}</span>
              <span className="inline-flex items-center gap-2 text-[12px]">
                <span className={`size-1.5 translate-y-[-1px] rounded-full ${dot}`} />
                {label}
              </span>
            </span>
            <span className="order-last col-span-2 min-w-0 sm:order-none sm:col-span-1 sm:truncate">
              <Link href={`/approvals?status=all&id=${item.refund_id}`} className={`num text-ink-2 ${ROW_LINK}`}>
                #{item.refund_id}
              </Link>{" "}
              {item.customer_name ?? "Unknown customer"} <span className="text-muted">· {item.item}</span>
              <span className="text-muted"> · {describe(item)}</span>
              {item.flagged && <span className="text-accent"> · linked accounts</span>}
            </span>
            <span className="num text-right">{money(item.amount_cents)}</span>
          </li>
        );
      })}
    </ol>
  );
}

function describe(item: ActivityItem): string {
  switch (item.event) {
    case "filed":
      return "by the agent";
    case "issued":
      return `by the payout worker${item.provider_refund_id ? `, ${item.provider_refund_id}` : ""}`;
    case "rejected":
      return `by ${item.actor}${item.note ? `: “${item.note}”` : ""}`;
    default:
      return `by ${item.actor}`;
  }
}

function Fact({ value, label }: { value: string; label: string }) {
  return (
    <div className="pt-1">
      <div className="num text-[22px] leading-none">{value}</div>
      <div className="mt-2 text-[12px] leading-snug text-muted">{label}</div>
    </div>
  );
}
