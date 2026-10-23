"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { StatusTag } from "@/components/ui";
import { money } from "@/lib/format";
import type { CustomerDetail } from "@/lib/types";

// A customer's name that opens their history: orders, how each was paid and shipped, and refund requests.
// The history is drawn in document.body at a fixed position, so a scrolling panel around the name cannot clip it.
const WIDTH = 360;
const GAP = 8;

type Place = { left: number; top?: number; bottom?: number; maxHeight: number; width: number };

export function CustomerLink({ id, name, className = "" }: { id: string | null; name: string | null; className?: string }) {
  const [place, setPlace] = useState<Place | null>(null);
  const open = place != null;
  const [detail, setDetail] = useState<CustomerDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!button.current?.contains(target) && !panel.current?.contains(target)) setPlace(null);
    };
    const escape = (e: KeyboardEvent) => e.key === "Escape" && setPlace(null);
    const moved = (e: Event) => !panel.current?.contains(e.target as Node) && setPlace(null);  // the name moved away
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", escape);
    window.addEventListener("scroll", moved, true);
    window.addEventListener("resize", moved);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", escape);
      window.removeEventListener("scroll", moved, true);
      window.removeEventListener("resize", moved);
    };
  }, [open]);

  if (!id) return <span className={className}>{name ?? "Unknown customer"}</span>;

  function toggle() {
    if (open || !button.current) {
      setPlace(null);
      return;
    }
    const at = button.current.getBoundingClientRect();
    const width = Math.min(WIDTH, window.innerWidth - GAP * 2);  // a phone is narrower than the history
    const left = Math.max(GAP, Math.min(at.left, window.innerWidth - width - GAP));
    const below = window.innerHeight - at.bottom - GAP * 2;
    const above = at.top - GAP * 2;
    // Below the name when the whole history fits there, otherwise on whichever side has more room.
    setPlace(below >= 520 || below >= above ? { left, width, top: at.bottom + GAP, maxHeight: below } : { left, width, bottom: window.innerHeight - at.top + GAP, maxHeight: above });
    if (detail || loading) return;
    setLoading(true);
    fetch(`/customers/${encodeURIComponent(id!)}`)
      .then(async (response) => {
        const body = await response.json();
        if (response.ok) setDetail(body);
        else setError(body.error ?? "The harness could not be reached.");
      })
      .catch(() => setError("The harness could not be reached."))
      .finally(() => setLoading(false));
  }

  return (
    <>
      <button
        ref={button}
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className={`underline decoration-line-strong decoration-dotted underline-offset-[3px] transition-colors hover:decoration-ink ${className}`}
      >
        {name ?? id}
      </button>
      {place &&
        createPortal(
          <div
            ref={panel}
            style={{ left: place.left, top: place.top, bottom: place.bottom, maxHeight: place.maxHeight, width: place.width }}
            className="rise fixed z-50 flex flex-col overflow-hidden border border-ink bg-surface text-left shadow-[0_8px_24px_-12px_rgba(20,20,19,0.35)]"
          >
            {error ? <p className="px-4 py-4 text-[12px] text-accent">{error}</p> : !detail ? <p className="px-4 py-4 text-[12px] text-muted">Loading…</p> : <History detail={detail} />}
          </div>,
          document.body,
        )}
    </>
  );
}

function History({ detail }: { detail: CustomerDetail }) {
  const { orders } = detail;
  const spent = orders.reduce((sum, o) => sum + o.total_cents, 0);
  const refunded = orders.filter((o) => o.refund_status === "issued").reduce((sum, o) => sum + (o.refund_cents ?? 0), 0);
  const requests = orders.filter((o) => o.refund_id != null).length;
  const cards = [...new Set(orders.map((o) => o.card_last4).filter(Boolean))];
  const addresses = [...new Set(orders.map((o) => o.address).filter(Boolean))];

  return (
    <>
      <div className="border-b border-line px-4 pb-3 pt-3.5">
        <div className="text-[14px] text-ink">{detail.name}</div>
        <div className="num mt-0.5 text-[11px] text-faint">{detail.customer_id}</div>
      </div>
      <dl className="grid grid-cols-3 border-b border-line">
        <Figure label="Orders" value={`${orders.length}`} sub={money(spent)} />
        <Figure label="Requests" value={`${requests}`} />
        <Figure label="Refunded" value={money(refunded)} />
      </dl>
      <dl className="grid grid-cols-[72px_1fr] gap-x-3 gap-y-1.5 border-b border-line px-4 py-3 text-[12px]">
        <dt className="text-muted">Cards</dt>
        <dd className="num text-ink-2">{cards.length ? cards.map((c) => `••${c}`).join(", ") : "—"}</dd>
        <dt className="text-muted">Ships to</dt>
        <dd className="text-ink-2">{addresses.length ? addresses.join("; ") : "—"}</dd>
      </dl>
      <ol className="min-h-0 flex-1 divide-y divide-line overflow-auto">
        {orders.map((o) => (
          <li key={o.order_id} className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-1 px-4 py-2.5 text-[12px]">
            <span className="truncate text-ink">
              {o.item} <span className="num text-faint">{o.order_id}</span>
            </span>
            <span className="num text-ink-2">{money(o.total_cents)}</span>
            {o.refund_id != null && o.refund_status ? (
              <Link href={`/approvals?status=all&id=${o.refund_id}`} scroll={false} className="col-span-2 flex items-center justify-between text-muted hover:text-ink">
                <span>
                  Refund <span className="num">#{o.refund_id}</span>, <span className="num">{money(o.refund_cents)}</span>
                </span>
                <StatusTag status={o.refund_status} />
              </Link>
            ) : (
              <span className="col-span-2 text-faint">No refund requested</span>
            )}
          </li>
        ))}
      </ol>
    </>
  );
}

function Figure({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="border-line px-4 py-3 [&:not(:first-child)]:border-l">
      <dt className="label mb-1.5 !text-[10px]">{label}</dt>
      <dd className="num text-[15px]">{value}</dd>
      {sub && <dd className="num mt-0.5 text-[11px] text-muted">{sub}</dd>}
    </div>
  );
}
