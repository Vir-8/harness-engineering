import type { ReactNode } from "react";

import type { LedgerLine, RefundStatus } from "@/lib/types";

export function PageTitle({ children, lede, aside }: { children: ReactNode; lede?: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-6 sm:mb-10">
      <div>
        <h1 className="text-[26px] font-medium leading-none tracking-[-0.03em] sm:text-[30px]">{children}</h1>
        {lede && <p className="mt-4 max-w-[640px] text-[14px] leading-relaxed text-ink-2">{lede}</p>}
      </div>
      {aside}
    </div>
  );
}

export function SectionTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-3 flex items-baseline justify-between gap-4 border-b border-line pb-2">
      <h2 className="label">{children}</h2>
      {aside && <div className="text-[12px] text-muted">{aside}</div>}
    </div>
  );
}

// A row of figures with dividers between them: 2 per row on small screens, 4 from lg up. Each child is padded and bordered
// by its position, so the dividers stay right however the row wraps.
export function StatGrid({ children }: { children: ReactNode }) {
  return (
    <div
      className={[
        "grid grid-cols-2 border-y border-line lg:grid-cols-4",
        "[&>*]:border-line [&>*]:px-4 sm:[&>*]:px-5",
        "[&>*:nth-child(odd)]:pl-0 [&>*:nth-child(even)]:border-l [&>*:nth-child(n+3)]:border-t",
        "lg:[&>*:nth-child(n+3)]:border-t-0 lg:[&>*:not(:first-child)]:border-l lg:[&>*:nth-child(odd):not(:first-child)]:pl-5",
      ].join(" ")}
    >
      {children}
    </div>
  );
}

export function Stat({ label, value, sub, tone, text }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "accent"; text?: boolean }) {
  return (
    <div className="min-w-0 py-5">
      <div className="label mb-3">{label}</div>
      <div className={`truncate text-[24px] leading-none sm:text-[28px] ${text ? "tracking-[-0.03em]" : "num"} ${tone === "accent" ? "text-accent" : ""}`}>{value}</div>
      {sub != null && <div className="num mt-2 text-[12px] text-muted">{sub}</div>}
    </div>
  );
}

const STATUS: Record<RefundStatus, { label: ReactNode; dot: string }> = {
  // On a phone the tag says just "Awaiting", so a table row still fits.
  pending: { label: <>Awaiting<span className="hidden sm:inline"> approval</span></>, dot: "bg-warn" },
  approved: { label: "Approved", dot: "bg-ink" },
  issued: { label: "Paid", dot: "bg-ok" },
  rejected: { label: "Rejected", dot: "bg-faint" },
};

export function StatusTag({ status }: { status: RefundStatus }) {
  const { label, dot } = STATUS[status];
  return (
    <span className="inline-flex items-center gap-2 whitespace-nowrap text-[12px]">
      <span className={`size-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

const LEDGER: Record<LedgerLine["state"], string> = {
  matched: "Matched",
  awaiting_payout: "Awaiting payout",
  paid_not_recorded: "Paid, not recorded",
  missing_at_provider: "Missing at provider",
  duplicate_payout: "Duplicate payout",
  amount_mismatch: "Amount mismatch",
  unknown_payout: "Unknown payout",
};

export function LedgerTag({ line }: { line: LedgerLine }) {
  return (
    <span className={`inline-flex items-center gap-2 whitespace-nowrap text-[12px] ${line.ok ? "" : "text-accent"}`}>
      <span className={`size-1.5 rounded-full ${line.ok ? (line.state === "matched" ? "bg-ok" : "bg-ink") : "bg-accent"}`} />
      {LEDGER[line.state]}
    </span>
  );
}

// On the one link in a table row (the row needs `relative`): its click area stretches over the whole row.
export const ROW_LINK = "after:absolute after:inset-0 after:content-[''] focus-visible:outline-none focus-visible:after:outline focus-visible:after:outline-1 focus-visible:after:outline-ink";

export function Th({ children, align = "left", className = "" }: { children?: ReactNode; align?: "left" | "right"; className?: string }) {
  return <th className={`label whitespace-nowrap py-2.5 pr-4 font-normal last:pr-0 ${align === "right" ? "text-right" : "text-left"} ${className}`}>{children}</th>;
}

// A table too wide for a phone scrolls sideways inside this, edge to edge, instead of pushing the page wider.
export function TableScroll({ children }: { children: ReactNode }) {
  return <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">{children}</div>;
}

export function Td({ children, align = "left", className = "" }: { children?: ReactNode; align?: "left" | "right"; className?: string }) {
  return <td className={`py-3 pr-4 align-middle last:pr-0 ${align === "right" ? "text-right" : ""} ${className}`}>{children}</td>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="border-b border-line py-10 text-[13px] text-muted">{children}</div>;
}

export function Unavailable({ children = "The harness could not be reached." }: { children?: ReactNode }) {
  return <div className="border border-dashed border-line-strong px-4 py-6 text-[13px] text-muted">{children}</div>;
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-px">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="pulse h-11 border-b border-line bg-surface/60" />
      ))}
    </div>
  );
}
