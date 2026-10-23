import { AutoRefresh } from "@/components/auto-refresh";
import { CustomerLink } from "@/components/customer-link";
import { DecisionButtons } from "@/components/decision-buttons";
import { StatusTag } from "@/components/ui";
import { ApiError, api, currentTenant } from "@/lib/api";
import { money, timestamp } from "@/lib/format";
import type { LinkedCustomer, LinkStep, RefundDetail } from "@/lib/types";

export async function RequestDetail({ id }: { id: number }) {
  let r: RefundDetail;
  const tenant = await currentTenant();
  try {
    r = await api.refund(id);
  } catch (error) {
    const missing = error instanceof ApiError && error.status === 404;
    return <div className="border border-line px-5 py-8 text-[13px] text-muted">{missing ? "No such request." : "The harness could not be reached."}</div>;
  }

  const approvedAt = r.events.find((e) => e.event === "approved");
  const rejectedAt = r.events.find((e) => e.event === "rejected");
  const paidAt = r.events.find((e) => e.event === "issued");

  return (
    // Beside the list (lg): as tall as its box allows, the body scrolls inside and the decision stays in view at the
    // bottom. In the phone sheet the sheet scrolls instead, and the decision sticks to the bottom of the screen.
    <div key={r.refund_id} className="rise flex flex-col border border-line bg-surface lg:max-h-full lg:min-h-0">
      {r.status === "approved" && <AutoRefresh busy />}
      <div className="lg:min-h-0 lg:flex-1 lg:overflow-y-auto lg:overscroll-contain">
        <div className="border-b border-line px-5 pb-5 pt-4">
          <div className="mb-4 flex items-center justify-between">
            <span className="num text-[12px] text-muted">#{r.refund_id}</span>
            <StatusTag status={r.status} />
          </div>
          <div className="num text-[34px] leading-none tracking-[-0.02em]">{money(r.amount_cents)}</div>
          <div className="mt-2 text-[13px] text-ink-2">
            <CustomerLink id={r.customer_id} name={r.customer_name} /> · {r.item}
          </div>
          {rejectedAt?.note && (
            <p className="mt-4 border-l-2 border-accent pl-3 text-[13px] leading-snug text-ink-2">
              {rejectedAt.note}
              <span className="mt-1 block text-[11px] text-muted">Rejected by {rejectedAt.actor}</span>
            </p>
          )}
        </div>

        <div className="border-b border-line px-5 py-4">
          <div className="label mb-2.5">Linked accounts</div>
          {r.risk_flags == null ? (
            <p className="text-[13px] text-muted">Not checked</p>
          ) : r.risk_flags.length === 0 ? (
            <p className="text-[13px] text-muted">None found</p>
          ) : (
            <>
              <ul className="space-y-2">
                {r.risk_flags.map((flag) => (
                  <li key={flag.code} className="flex gap-2.5 text-[13px] leading-snug">
                    <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />
                    {flag.message}
                  </li>
                ))}
              </ul>
              <Links customer={r.customer_name} links={r.risk_flags.flatMap((flag) => flag.links ?? [])} />
            </>
          )}
        </div>

        <Lifecycle
          steps={[
            { label: "Requested", at: r.created_at, by: "agent" },
            rejectedAt
              ? { label: "Rejected", at: rejectedAt.created_at, by: rejectedAt.actor }
              : { label: "Approved", at: approvedAt?.created_at, by: approvedAt?.actor },
            { label: "Paid", at: paidAt?.created_at, by: r.provider_refund_id ?? undefined },
          ]}
        />

        <dl className="grid grid-cols-[120px_1fr] gap-y-2.5 border-t border-line px-5 py-4 text-[13px]">
          <dt className="text-muted">Order</dt>
          <dd className="num">{r.order_id}</dd>
          <dt className="text-muted">Order total</dt>
          <dd className="num">{money(r.order_total_cents)}</dd>
          <dt className="text-muted">Idempotency key</dt>
          <dd className="num truncate text-ink-2">{`${tenant}:refund-${r.refund_id}`}</dd>
          <dt className="text-muted">Provider payout</dt>
          <dd className="num truncate text-ink-2">{r.provider_refund_id ?? "—"}</dd>
        </dl>

        <div className="border-t border-line px-5 py-4">
          <div className="label mb-2.5">Audit trail</div>
          <ol className="space-y-1.5 text-[12px]">
            <li className="grid grid-cols-[132px_1fr] gap-3">
              <span className="num whitespace-nowrap text-muted">{timestamp(r.created_at)}</span>
              <span>requested</span>
            </li>
            {r.events.map((e, i) => (
              <li key={i} className="grid grid-cols-[132px_1fr] gap-3">
                <span className="num whitespace-nowrap text-muted">{timestamp(e.created_at)}</span>
                <span>
                  {e.event} <span className="text-muted">by {e.actor}</span>
                  {e.note && <span className="mt-0.5 block text-muted">“{e.note}”</span>}
                </span>
              </li>
            ))}
          </ol>
        </div>
      </div>

      {r.status === "pending" && (
        <div className="sticky bottom-0 border-t border-line-strong bg-surface px-5 py-4 lg:static">
          <DecisionButtons refundId={r.refund_id} amount={money(r.amount_cents)} />
        </div>
      )}
    </div>
  );
}

function Links({ customer, links }: { customer: string | null; links: LinkedCustomer[] }) {
  if (links.length === 0) return null;
  return (
    <ol className="mt-4 space-y-3 border-t border-line pt-3.5">
      {links.map((link) => (
        <li key={link.customer_id} className="text-[12px]">
          <div className="flex items-baseline justify-between gap-3">
            <CustomerLink id={link.customer_id} name={link.name} className="text-[13px] text-ink" />
            <span className={`num whitespace-nowrap ${link.refunds ? "text-accent" : "text-faint"}`}>
              {link.refunds === 0 ? "no refunds" : link.refunds === 1 ? "1 refund" : `${link.refunds} refunds`}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-muted">
            <span>{customer ?? "This customer"}</span>
            {link.via.map((step, i) => (
              <span key={i} className="contents">
                <span className="text-faint">→</span>
                <Step step={step} />
              </span>
            ))}
            <span className="text-faint">→</span>
            <span>{link.name ?? link.customer_id}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

function Step({ step }: { step: LinkStep }) {
  if (step.kind === "customer") return <span>{step.label}</span>;
  return (
    <span className="num border border-line-strong bg-paper px-1.5 py-px text-[11px] text-ink-2">
      {step.kind === "card" ? `card ••${step.label}` : step.label}
    </span>
  );
}

function Lifecycle({ steps }: { steps: { label: string; at?: string; by?: string }[] }) {
  return (
    <ol className="grid grid-cols-3">
      {steps.map((step, i) => {
        const done = !!step.at;
        const current = done && !steps[i + 1]?.at;
        return (
          <li key={step.label} className={`border-line px-5 py-4 ${i > 0 ? "border-l" : ""}`}>
            <div className="mb-2 flex items-center gap-2">
              <span className={`size-1.5 rounded-full ${done ? (step.label === "Rejected" ? "bg-accent" : current ? "bg-ink" : "bg-ok") : "border border-line-strong"}`} />
              <span className={`text-[12px] ${done ? "text-ink" : "text-faint"}`}>{step.label}</span>
            </div>
            <div className="num truncate text-[11px] text-muted">{done ? timestamp(step.at!).split(", ")[1] : "—"}</div>
            {step.by && done && <div className="num mt-0.5 truncate text-[11px] text-faint">{step.by}</div>}
          </li>
        );
      })}
    </ol>
  );
}
