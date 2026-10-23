import type { ReactNode } from "react";

import { SwipeHint } from "@/components/system-map";

// A refund's life as a swimlane flowchart: who acts in each lane, the decisions, the statuses Postgres stores (the same
// names and colours as the rest of the console), and the failures the harness handles, dashed in red.

const LANES = [
  { name: "Customer", role: "asks", y: 0, h: 72 },
  { name: "Model", role: "proposes", y: 72, h: 72, model: true },
  { name: "Harness code", role: "decides", y: 144, h: 110 },
  { name: "Reviewer", role: "approves", y: 254, h: 82 },
  { name: "Worker", role: "pays", y: 336, h: 134 },
  { name: "Payment provider", role: "external", y: 470, h: 72 },
];
const HEIGHT = 542;

const STATUS = {
  pending: { label: "Awaiting approval", color: "var(--color-warn)" },
  approved: { label: "Approved", color: "var(--color-ink)" },
  issued: { label: "Paid", color: "var(--color-ok)" },
  rejected: { label: "Rejected", color: "var(--color-faint)" },
};

const LABELS = 128;  // the width of the lane-name column, in the diagram's units
const WIDTH = 1100;

function LaneBands({ from, to }: { from: number; to: number }) {
  return (
    <>
      {LANES.map((lane, i) => (
        <g key={lane.name}>
          <rect x={from} y={lane.y} width={to - from} height={lane.h} fill={lane.model ? "var(--color-model-soft)" : "var(--color-surface)"} />
          {i > 0 && <line x1={from} y1={lane.y} x2={to} y2={lane.y} stroke="var(--color-line)" />}
        </g>
      ))}
    </>
  );
}

// Two drawings side by side, in the same units: the lane names, and the lanes. On a narrow screen the lanes scroll
// sideways at a readable size while the names stay pinned at the left edge, so a lane is never anonymous.
export function Lifecycle() {
  return (
    <div className="border border-faint bg-surface">
      <div className="overflow-x-auto overscroll-x-contain">
        <div className="flex min-w-[1000px] lg:min-w-0">
          <svg
            viewBox={`0 0 ${LABELS} ${HEIGHT}`}
            className="sticky left-0 z-10 h-auto shrink-0"
            style={{ width: `${(LABELS / WIDTH) * 100}%` }}
            aria-hidden
          >
            <LaneBands from={0} to={LABELS} />
            {LANES.map((lane) => (
              <g key={lane.name}>
                <text x="16" y={lane.y + lane.h / 2 - 3} fontSize="12" fill={lane.model ? "var(--color-model)" : "var(--color-ink)"}>
                  {lane.name}
                </text>
                <text x="16" y={lane.y + lane.h / 2 + 13} fontSize="10" letterSpacing="0.06em" className="num uppercase" fill={lane.model ? "var(--color-model)" : "var(--color-muted)"}>
                  {lane.role}
                </text>
              </g>
            ))}
            <line x1={LABELS - 0.5} y1="0" x2={LABELS - 0.5} y2={HEIGHT} stroke="var(--color-line)" />
          </svg>
          <svg
            viewBox={`${LABELS} 0 ${WIDTH - LABELS} ${HEIGHT}`}
            className="h-auto shrink-0"
            style={{ width: `${((WIDTH - LABELS) / WIDTH) * 100}%` }}
            role="img"
            aria-label="The life of a refund, lane by lane"
          >
              <defs>
                <marker id="life-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0,0 L10,5 L0,10 z" fill="var(--color-ink-2)" />
                </marker>
                <marker id="life-arrow-fail" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0,0 L10,5 L0,10 z" fill="var(--color-accent)" />
                </marker>
              </defs>

              <LaneBands from={LABELS} to={WIDTH} />

              {/* the path, under the boxes */}
              <Path d="M200,56 V108 H290" />
              <Path d="M430,108 H455" />
              <Path d="M525,128 V165" />
              <Path d="M481,199 H440" />
              <Note x={462} y={192} end>no</Note>
              <Path d="M569,199 H595" />
              <Note x={572} y={192}>yes</Note>
              <Path d="M660,210 V261" />
              <Path d="M760,295 H704" dashed />
              <Path d="M616,295 H575" />
              <Note x={606} y={288} end>no</Note>
              <Path d="M660,329 V342" />
              <Note x={668} y={339}>yes</Note>
              <Path d="M660,364 V380" />
              <Path d="M735,398 H825 V488" />
              <Path d="M895,496 H980 V411" />
              <Path d="M980,389 V221" />
              <Path d="M895,514 H1060 V221" dashed />
              <Note x={1022} y={508} middle>their ledger</Note>

              {/* failures the harness handles */}
              <Path d="M640,416 V442 H690 V416" fail />
              <Note x={812} y={458} end fail>
                crash after paying → retry, same key → same payout
              </Note>
              <Path d="M585,398 H440" fail />
              <Note x={512} y={390} middle fail>
                fails 3 times
              </Note>

              {/* the steps */}
              <Box x={135} y={16} w={130} rounded>Asks for a refund</Box>
              <Box x={290} y={88} w={140} model>Reads order, policy</Box>
              <Box x={455} y={88} w={140} model>Files a request</Box>
              <Diamond cx={525} cy={199} w={88} h={68} lines={["Eligible, not", "a duplicate?"]} />
              <Box x={290} y={181} w={150} rounded sub="customer told why">Refused in code</Box>
              <Pill x={595} y={188} w={130} status="pending" />
              <Diamond cx={660} cy={295} w={88} h={68} lines={["Approve?"]} />
              <Pill x={455} y={284} w={120} status="rejected" />
              <Box x={760} y={276} w={160} h={38} sub="Neo4j">Linked-account notes</Box>
              <Note x={515} y={321} middle>with a reason</Note>
              <Pill x={605} y={342} w={110} status="approved" />
              <Note x={598} y={357} end>+ payout job (same transaction)</Note>
              <Box x={585} y={380} w={150} sub="idempotency key">Pays</Box>
              <Box x={300} y={380} w={140} fail sub="alert">Dead letters</Box>
              <Box x={755} y={488} w={140}>Pays once per key</Box>
              <Pill x={925} y={389} w={110} status="issued" />
              <Box x={930} y={177} w={150} h={44} rounded sub="ours against theirs">Reconciled</Box>
          </svg>
        </div>
      </div>
      <SwipeHint />
      <Legend />
    </div>
  );
}

function Tag({ status }: { status: keyof typeof STATUS }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap border border-faint px-1.5 text-[12px] text-ink">
      <span className="size-1.5 rounded-full" style={{ background: STATUS[status].color }} />
      {STATUS[status].label}
    </span>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-8 gap-y-2 border-t border-line px-5 py-3 text-[12px] text-ink-2">
      <span className="inline-flex items-center gap-2">
        <svg width="28" height="8" aria-hidden>
          <line x1="0" y1="4" x2="28" y2="4" stroke="var(--color-ink-2)" strokeWidth="1.2" />
        </svg>
        the path
      </span>
      <span className="inline-flex items-center gap-2">
        <svg width="28" height="8" aria-hidden>
          <line x1="0" y1="4" x2="28" y2="4" stroke="var(--color-accent)" strokeWidth="1.2" strokeDasharray="4 3" />
        </svg>
        a failure, and what the harness does about it
      </span>
      <span className="inline-flex items-center gap-2">
        <Tag status="pending" /> a status stored in Postgres, as the console shows it
      </span>
    </div>
  );
}

function Path({ d, dashed, fail }: { d: string; dashed?: boolean; fail?: boolean }) {
  return (
    <path
      d={d}
      fill="none"
      stroke={fail ? "var(--color-accent)" : dashed ? "var(--color-muted)" : "var(--color-ink-2)"}
      strokeWidth="1.2"
      strokeDasharray={fail || dashed ? "4 3" : undefined}
      markerEnd={fail ? "url(#life-arrow-fail)" : "url(#life-arrow)"}
    />
  );
}

function Box({ x, y, w, h = 40, rounded, fail, model, sub, children }: { x: number; y: number; w: number; h?: number; rounded?: boolean; fail?: boolean; model?: boolean; sub?: string; children: ReactNode }) {
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        rx={rounded ? h / 2 : 0}
        fill={model ? "var(--color-surface)" : "var(--color-paper)"}
        stroke={fail ? "var(--color-accent)" : model ? "var(--color-model)" : "var(--color-muted)"}
        strokeDasharray={fail ? "4 3" : undefined}
      />
      <text x={x + w / 2} y={sub ? y + h / 2 - 2 : y + h / 2 + 4.5} textAnchor="middle" fontSize="12.5" fill={fail ? "var(--color-accent)" : "var(--color-ink)"}>
        {children}
      </text>
      {sub && (
        <text x={x + w / 2} y={y + h / 2 + 12} textAnchor="middle" fontSize="10" className="num" fill="var(--color-muted)">
          {sub}
        </text>
      )}
    </g>
  );
}

function Diamond({ cx, cy, w, h, lines }: { cx: number; cy: number; w: number; h: number; lines: string[] }) {
  return (
    <g>
      <path d={`M${cx},${cy - h / 2} L${cx + w / 2},${cy} L${cx},${cy + h / 2} L${cx - w / 2},${cy} Z`} fill="var(--color-paper)" stroke="var(--color-ink)" />
      {lines.map((line, i) => (
        <text key={line} x={cx} y={cy + 4 + (i - (lines.length - 1) / 2) * 13} textAnchor="middle" fontSize="11" fill="var(--color-ink)">
          {line}
        </text>
      ))}
    </g>
  );
}

function Pill({ x, y, w, status }: { x: number; y: number; w: number; status: keyof typeof STATUS }) {
  const s = STATUS[status];
  return (
    <g>
      <rect x={x} y={y} width={w} height={22} rx={11} fill="var(--color-paper)" stroke="var(--color-ink)" />
      <circle cx={x + 14} cy={y + 11} r={3.5} fill={s.color} />
      <text x={x + 24} y={y + 15} fontSize="11.5" fill="var(--color-ink)">
        {s.label}
      </text>
    </g>
  );
}

function Note({ x, y, middle, end, fail, children }: { x: number; y: number; middle?: boolean; end?: boolean; fail?: boolean; children: ReactNode }) {
  return (
    <text x={x} y={y} fontSize="10" textAnchor={middle ? "middle" : end ? "end" : "start"} className="num" fill={fail ? "var(--color-accent)" : "var(--color-muted)"}>
      {children}
    </text>
  );
}
