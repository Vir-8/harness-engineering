"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

import { PARTS, type Part } from "@/data/architecture";
import { sourceUrl } from "@/lib/repo";

const BY_ID = new Map(PARTS.map((p) => [p.id, p]));

// The whole harness on one screen: a diagram that draws the boundaries that matter, what the model can reach, what
// commits in one transaction, where the work stops being synchronous. Choosing a part shows what it does and where its
// code is. On a narrow screen it is the same diagram at a readable size, swiped sideways.
export function SystemMap() {
  const [selected, setSelected] = useState("loop");
  const frame = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Swiped sideways on a narrow screen, the diagram starts centred on the agent loop, what the model can reach,
    // rather than at its left edge. Done once, the first time the diagram is laid out wider than the frame: that can be
    // after this runs (before the stylesheet applies), so it watches the diagram's own width, which is what changes.
    const el = frame.current;
    const content = el?.firstElementChild;
    if (!el || !content) return;
    let done = false;
    const centre = () => {
      if (done || el.scrollWidth <= el.clientWidth) return;
      el.scrollLeft = (480 / 1100) * el.scrollWidth - el.clientWidth / 2;
      done = true;
    };
    const watch = new ResizeObserver(centre);
    watch.observe(content);
    centre();
    return () => watch.disconnect();
  }, []);
  return (
    <div className="border border-faint bg-surface">
      <div ref={frame} className="overflow-x-auto overscroll-x-contain px-4 pb-2 pt-4 sm:px-5">
        <div className="min-w-[960px] lg:min-w-0">
          <Flowchart selected={selected} onSelect={setSelected} />
        </div>
      </div>
      <SwipeHint />
      <Detail part={BY_ID.get(selected)!} />
    </div>
  );
}

type Select = { selected: string; onSelect: (id: string) => void };

// --- the diagram ---

function Flowchart({ selected, onSelect }: Select) {
  const node = (id: string, x: number, y: number, w: number, label: string, sub?: string, h = 48, tone?: Tone) => (
    <Node key={`${id}-${x}-${y}`} id={id} x={x} y={y} w={w} h={h} label={label} sub={sub} tone={tone} active={selected === id} onSelect={onSelect} />
  );
  return (
    <svg viewBox="0 0 1100 600" className="h-auto w-full" role="group" aria-label="The harness, with what the model can reach">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--color-muted)" />
        </marker>
        <marker id="arrow-model" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--color-model)" />
        </marker>
        <marker id="arrow-fail" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--color-accent)" />
        </marker>
      </defs>

      {/* outside the harness: the model provider above, the payment provider to the right */}
      {node("model", 330, 10, 140, "Model provider", "OpenAI", 48, "model")}
      {node("provider", 962, 372, 134, "Payment provider", "stand-in, Stripe-like")}

      {/* the harness itself */}
      <rect x="150" y="90" width="725" height="432" fill="none" stroke="var(--color-faint)" />
      <Caption x={164} y={108}>The harness</Caption>

      {/* what the model can reach: the loop and three tools, nothing else */}
      <rect x="312" y="126" width="330" height="96" fill="var(--color-model-soft)" stroke="var(--color-model)" strokeDasharray="5 4" />
      <Caption x={412} y={143} color="var(--color-model)">
        What the model can reach
      </Caption>

      {/* one transaction: status, audit event and payout job, all or nothing */}
      <rect x="175" y="345" width="295" height="104" fill="var(--color-ok-soft)" stroke="var(--color-ok)" />
      <Caption x={187} y={362} color="var(--color-ok)">
        One transaction
      </Caption>

      {/* the asynchronous side: everything after the approval commits */}
      <rect x="488" y="338" width="380" height="176" fill="var(--color-warn-soft)" />
      <line x1="488" y1="338" x2="488" y2="514" stroke="var(--color-warn)" strokeDasharray="3 4" />
      <Caption x={496} y={354} color="var(--color-warn-ink)">
        async
      </Caption>

      {/* arrows first, so the nodes draw over their ends */}
      <Arrow d="M125,182 H175" />
      <Arrow d="M285,182 H330" />
      <Arrow d="M470,182 H500" />
      <Arrow d="M400,158 V58" both tone="model" />
      <Label x={408} y={112} color="var(--color-model)">messages, tool calls</Label>
      <Arrow d="M562,206 L400,262" />
      <Arrow d="M562,206 V262" />
      <Arrow d="M562,206 L722,262" />
      <Arrow d="M125,396 H175" />
      <Arrow d="M400,345 V310" />
      <Label x={408} y={333}>commits</Label>
      <Arrow d="M470,396 H505" />
      <Arrow d="M605,396 H630" />
      <Arrow d="M735,396 H760" />
      <Arrow d="M855,396 H962" />
      <Label x={920} y={388} middle small>
        Idempotency-Key
      </Label>
      <Arrow d="M682,420 V462" tone="fail" />
      <Label x={690} y={446} color="var(--color-accent)">3 tries</Label>
      <Arrow d="M1029,420 V484 H862" dashed />
      <Label x={1021} y={500} end>
        their ledger
      </Label>

      {node("console", 10, 158, 115, "Console", "customer's message")}
      {node("api", 175, 158, 110, "API", "FastAPI")}
      {node("loop", 330, 158, 140, "Agent loop", "136 lines", 48, "model")}
      {node("tools", 500, 158, 124, "Tools", "3 functions", 48, "model")}
      {node("postgres", 330, 262, 140, "Postgres", "+ pgvector")}
      {node("redis", 500, 262, 124, "Redis", "cache · rate limit")}
      {node("neo4j", 660, 262, 124, "Neo4j", "linked accounts")}

      {node("reviewer", 10, 372, 115, "Reviewer", "approve · reject")}
      <Chip x={187} y={376} w={78}>status</Chip>
      <Chip x={273} y={376} w={92}>audit event</Chip>
      <Chip x={373} y={376} w={85}>outbox row</Chip>
      <TxTarget active={selected === "approval"} onSelect={onSelect} />
      {node("relay", 505, 372, 100, "Relay", "outbox → queue")}
      {node("queue", 630, 372, 105, "Queue", "SQS")}
      {node("worker", 760, 372, 95, "Worker", "pays")}
      {node("queue", 630, 462, 105, "Dead letters", undefined, 40, "fail")}
      {node("reconcile", 752, 462, 110, "Reconciliation", undefined, 44)}

      {/* watching all of it */}
      <Caption x={10} y={574}>Watching</Caption>
      {node("traces", 175, 548, 160, "Traces", "OpenTelemetry · Sentry", 46)}
      {node("metrics", 350, 548, 160, "Metrics", "Prometheus · Grafana", 46)}
      {node("evals", 525, 548, 160, "Evals", "scored in code", 46)}
      {node("load", 700, 548, 160, "Load", "k6", 46)}
    </svg>
  );
}

// A part drawn in a boundary's colour: blue for the model's side, red for where failures end up.
type Tone = "model" | "fail";

function Node({ id, x, y, w, h, label, sub, tone, active, onSelect }: { id: string; x: number; y: number; w: number; h: number; label: string; sub?: string; tone?: Tone; active: boolean; onSelect: (id: string) => void }) {
  const color = tone === "model" ? "var(--color-model)" : tone === "fail" ? "var(--color-accent)" : null;
  return (
    <g
      role="button"
      tabIndex={0}
      aria-pressed={active}
      aria-label={label}
      onClick={() => onSelect(id)}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect(id))}
      className="cursor-pointer outline-none [&:focus-visible>rect]:stroke-ink [&:hover>rect]:stroke-ink"
    >
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        fill={active ? "var(--color-ink)" : "var(--color-surface)"}
        stroke={active ? "var(--color-ink)" : (color ?? "var(--color-muted)")}
        strokeDasharray={tone === "fail" && !active ? "4 3" : undefined}
      />
      <text x={x + w / 2} y={sub ? y + h / 2 - 3 : y + h / 2 + 4.5} textAnchor="middle" fontSize="13" fill={active ? "var(--color-paper)" : tone === "fail" ? color! : "var(--color-ink)"}>
        {label}
      </text>
      {sub && (
        <text x={x + w / 2} y={y + h / 2 + 13} textAnchor="middle" fontSize="10" className="num" fill={active ? "var(--color-line)" : (color ?? "var(--color-muted)")}>
          {sub}
        </text>
      )}
    </g>
  );
}

// The transaction box is chosen as a whole ("approval"): an invisible target over it, under its label.
function TxTarget({ active, onSelect }: { active: boolean; onSelect: (id: string) => void }) {
  return (
    <g
      role="button"
      tabIndex={0}
      aria-pressed={active}
      aria-label="Approval, one transaction"
      onClick={() => onSelect("approval")}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect("approval"))}
      className="cursor-pointer outline-none [&:focus-visible>rect]:stroke-ink [&:hover>rect]:stroke-ink"
    >
      <rect x="187" y="414" width="271" height="26" fill={active ? "var(--color-ink)" : "var(--color-surface)"} stroke={active ? "var(--color-ink)" : "var(--color-muted)"} />
      <text x="322" y="431" textAnchor="middle" fontSize="11" fill={active ? "var(--color-paper)" : "var(--color-ink-2)"}>
        Approval: all three or none
      </text>
    </g>
  );
}

function Chip({ x, y, w, children }: { x: number; y: number; w: number; children: ReactNode }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={28} fill="var(--color-surface)" stroke="var(--color-muted)" />
      <text x={x + w / 2} y={y + 18} textAnchor="middle" fontSize="11" className="num" fill="var(--color-ink-2)">
        {children}
      </text>
    </g>
  );
}

function Arrow({ d, both, dashed, tone }: { d: string; both?: boolean; dashed?: boolean; tone?: Tone }) {
  const marker = `url(#${tone ? `arrow-${tone}` : "arrow"})`;
  return (
    <path
      d={d}
      fill="none"
      stroke={tone === "model" ? "var(--color-model)" : tone === "fail" ? "var(--color-accent)" : "var(--color-muted)"}
      strokeWidth="1.2"
      strokeDasharray={dashed || tone === "fail" ? "4 3" : undefined}
      markerEnd={marker}
      markerStart={both ? marker : undefined}
    />
  );
}

function Caption({ x, y, color = "var(--color-muted)", children }: { x: number; y: number; color?: string; children: ReactNode }) {
  return (
    <text x={x} y={y} fontSize="10" letterSpacing="0.06em" className="num uppercase" fill={color}>
      {children}
    </text>
  );
}

function Label({ x, y, middle, end, small, color = "var(--color-muted)", children }: { x: number; y: number; middle?: boolean; end?: boolean; small?: boolean; color?: string; children: ReactNode }) {
  return (
    <text x={x} y={y} fontSize={small ? 9 : 10} textAnchor={middle ? "middle" : end ? "end" : "start"} className="num" fill={color}>
      {children}
    </text>
  );
}

// --- what the chosen part does ---

function Detail({ part }: { part: Part }) {
  return (
    <div key={part.id} className="rise grid grid-cols-1 gap-x-10 gap-y-4 border-t border-ink px-4 py-5 sm:px-5 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div>
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 className="text-[17px] tracking-[-0.01em] text-ink">{part.name}</h3>
          <span className="num text-[12px] text-muted">{part.tech}</span>
        </div>
        <p className="mt-2.5 max-w-[640px] text-[14px] leading-relaxed text-ink-2">{part.what}</p>
      </div>
      <dl className="text-[12px]">
        <dt className="label mb-1 !text-[10px]">Source</dt>
        <dd>
          <a href={sourceUrl(part.source)} target="_blank" rel="noreferrer" className="num break-all text-ink-2 hover:text-ink">
            {part.source} ↗
          </a>
        </dd>
      </dl>
    </div>
  );
}

export function SwipeHint() {
  return <p className="border-t border-line px-4 py-2 text-center text-[11px] text-muted lg:hidden">Swipe sideways to see the whole diagram</p>;
}
