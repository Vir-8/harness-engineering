"use client";

import Link from "next/link";
import { useEffect, useState, useTransition } from "react";

import { ask } from "@/app/actions";
import { StatGrid } from "@/components/ui";
import { count, duration, money, tinyMoney } from "@/lib/format";
import type { AskResponse, Step } from "@/lib/types";

export function AgentConsole({ examples }: { examples: readonly string[] }) {
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const [result, setResult] = useState<AskResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, start] = useTransition();

  function run(text: string) {
    const q = text.trim();
    if (q.length < 2 || running) return;
    setAsked(q);
    setError(null);
    setResult(null);
    start(async () => {
      const response = await ask(q);
      if (response.ok) setResult(response.data);
      else setError(response.error);
    });
  }

  return (
    <div className="grid grid-cols-1 gap-10 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-12">
      <div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            run(question);
          }}
          className="border border-line-strong bg-surface focus-within:border-ink"
        >
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(question);
            }}
            rows={4}
            maxLength={1000}
            placeholder="A customer's message"
            className="block w-full resize-none bg-transparent px-4 py-3.5 text-[14px] leading-relaxed outline-none placeholder:text-faint"
          />
          <div className="flex items-center justify-between border-t border-line pl-4">
            <span className="label !text-[10px]">⌘ ↵</span>
            <button
              type="submit"
              disabled={running || question.trim().length < 2}
              className="group flex items-stretch border-l border-line text-[13px] transition-colors hover:bg-ink hover:text-paper disabled:pointer-events-none disabled:text-faint"
            >
              <span className="px-4 py-2.5">{running ? "Running" : "Run"}</span>
              <span className="flex items-center border-l border-dashed border-line px-3.5 transition-transform group-hover:translate-x-0.5">→</span>
            </button>
          </div>
        </form>

        <ul className="mt-6 divide-y divide-line border-y border-line">
          {examples.map((example) => (
            <li key={example}>
              <button
                type="button"
                onClick={() => {
                  setQuestion(example);
                  run(example);
                }}
                disabled={running}
                className="w-full py-3 text-left text-[13px] text-ink-2 transition-colors hover:text-ink disabled:text-faint"
              >
                {example}
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="min-w-0">
        {!asked && <Boundary />}
        {asked && (
          <div key={asked + (result ? "done" : "")}>
            <p className="mb-6 border-l-2 border-line-strong pl-4 text-[14px] leading-relaxed text-ink-2">{asked}</p>
            {running && <Running />}
            {error && <p className="border border-accent/40 px-4 py-3 text-[13px] text-accent">{error}</p>}
            {result && <Run result={result} />}
          </div>
        )}
      </div>
    </div>
  );
}

// What the harness hands the model, and what it deliberately does not (src/refund_harness/refund_tools.py).
const GIVEN = [
  { name: "get_order", what: "Looks up an order by ID, within the current merchant." },
  { name: "search_policy", what: "Searches the merchant's refund policy. Weak matches are dropped." },
  { name: "submit_order_refund", what: "Checks eligibility and creates a pending refund request." },
];

const WITHHELD = [
  { name: "Approving or paying", what: "Done by a reviewer and the payout worker." },
  { name: "Choosing the merchant", what: "Set by the harness from the request." },
  { name: "Linked-account notes", what: "Shown to reviewers only." },
];

function Boundary() {
  return (
    <div className="border border-line-strong bg-surface">
      <BoundaryList title="Tools the model has" items={GIVEN} mono />
      <BoundaryList title="Not available to the model" items={WITHHELD} muted />
      <p className="border-t border-line px-5 py-3.5 text-[12px] text-muted">Run a message to see each step.</p>
    </div>
  );
}

function BoundaryList({ title, items, mono, muted }: { title: string; items: { name: string; what: string }[]; mono?: boolean; muted?: boolean }) {
  return (
    <div className="border-b border-line px-5 pb-4 pt-4 last:border-b-0">
      <div className="label mb-3">{title}</div>
      <dl className="space-y-2.5">
        {items.map((item) => (
          <div key={item.name} className="grid grid-cols-1 gap-x-4 gap-y-0.5 text-[13px] sm:grid-cols-[150px_minmax(0,1fr)]">
            <dt className={`${mono ? "num" : ""} ${muted ? "text-muted" : ""}`}>{item.name}</dt>
            <dd className="text-ink-2">{item.what}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Running() {
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(started);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="flex items-center gap-3 border-y border-line py-3 text-[13px] text-muted">
      <span className="pulse size-1.5 rounded-full bg-ink" />
      Working
      <span className="num ml-auto">{duration(now - started)}</span>
    </div>
  );
}

const PROPOSED = /Refund request (\d+) for order (\S+) \(\$([\d.]+)\)/;

function Run({ result }: { result: AskResponse }) {
  const longest = Math.max(...result.steps.map((s) => s.duration_ms), 1);
  const proposal = result.steps
    .filter((s) => s.kind === "tool" && s.name === "submit_order_refund")
    .map((s) => s.output.match(PROPOSED))
    .find(Boolean);

  return (
    <div className="space-y-8">
      <div className="rise">
        <p className="whitespace-pre-line text-[15px] leading-relaxed">{result.answer}</p>
        {proposal && (
          <Link
            href={`/approvals?status=pending&id=${proposal[1]}`}
            className="group mt-5 flex items-center justify-between border border-ink px-4 py-3 text-[13px] transition-colors hover:bg-ink hover:text-paper"
          >
            <span>
              Request <span className="num">#{proposal[1]}</span> for {money(Math.round(Number(proposal[3]) * 100))} is awaiting approval
            </span>
            <span className="transition-transform group-hover:translate-x-0.5">→</span>
          </Link>
        )}
      </div>

      <div>
        <StatGrid>
          <Total label="Time" value={duration(result.duration_ms)} />
          <Total label="Steps" value={String(result.steps.length)} />
          <Total label="Tokens" value={`${count(result.input_tokens)} / ${count(result.output_tokens)}`} />
          <Total label="Cost" value={tinyMoney(result.cost_usd)} />
        </StatGrid>
        <ol>
          {result.steps.map((step, i) => (
            <StepRow key={i} step={step} index={i} longest={longest} />
          ))}
        </ol>
      </div>
    </div>
  );
}

function Total({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 py-3">
      <div className="label mb-1.5 !text-[10px]">{label}</div>
      <div className="num text-[15px]">{value}</div>
    </div>
  );
}

function StepRow({ step, index, longest }: { step: Step; index: number; longest: number }) {
  const [open, setOpen] = useState(false);
  const detail = step.kind === "tool" ? step.output : step.output || "";
  return (
    <li className="rise border-b border-line" style={{ animationDelay: `${index * 70}ms` }}>
      <button type="button" onClick={() => setOpen((o) => !o)} className="group grid w-full grid-cols-[28px_52px_minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-2 pb-2.5 pt-3 text-left">
        <span className="num text-[11px] text-faint">{String(index + 1).padStart(2, "0")}</span>
        <span className={`label !text-[10px] ${step.failed ? "!text-accent" : ""}`}>{step.kind}</span>
        <span className="min-w-0 truncate text-[13px]">
          {step.kind === "model" ? (
            step.tool_calls.length ? (
              <>
                <span className="text-muted">calls </span>
                {step.tool_calls.map((c, j) => (
                  <span key={c.id} className="num">
                    {j > 0 && <span className="text-muted">, </span>}
                    {c.name}
                  </span>
                ))}
              </>
            ) : (
              <span className="text-muted">answers</span>
            )
          ) : (
            <>
              <span className="num">{step.name}</span>
              <span className="num text-muted"> {formatArgs(step.arguments)}</span>
            </>
          )}
        </span>
        <span className="num text-right text-[12px] text-muted">
          {step.usage && (
            <span className="mr-4 hidden text-faint sm:inline">
              {count(step.usage.input_tokens)} → {count(step.usage.output_tokens)}
            </span>
          )}
          {duration(step.duration_ms)}
        </span>
        <span className="col-span-4 col-start-1 h-px bg-transparent">
          <span className={`block h-px ${step.kind === "model" ? "bg-ink/70" : "bg-ok/70"}`} style={{ width: `${Math.max(1, (step.duration_ms / longest) * 100)}%` }} />
        </span>
      </button>
      {open && detail && (
        <pre className="num mb-3 ml-0 max-h-72 sm:ml-[92px] overflow-auto whitespace-pre-wrap border-l border-line pl-4 text-[12px] leading-relaxed text-ink-2">{detail}</pre>
      )}
    </li>
  );
}

function formatArgs(args: Record<string, unknown> | null): string {
  if (!args) return "";
  return Object.values(args)
    .map((v) => (typeof v === "string" ? `"${v}"` : String(v)))
    .join(", ");
}
