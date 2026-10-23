import type { Metadata } from "next";

import { PageTitle, SectionTitle, Stat, StatGrid, TableScroll, Td, Th } from "@/components/ui";
import quality from "@/data/quality.json";
import { tinyMoney } from "@/lib/format";

export const metadata: Metadata = { title: "Quality" };

const CATEGORY: Record<string, string> = {
  policy: "Policy questions",
  paraphrase: "Paraphrased questions",
  order: "Order status",
  refund: "Refund decisions",
  tenant: "Tenant isolation",
  abstain: "Out of scope",
  safety: "Approval safety",
  multi: "Multi-part",
};

const CHECK: Record<string, string> = {
  completed: "Finished without error",
  tools_called: "Called the expected tools",
  tools_not_called: "Avoided forbidden tools",
  agent_found_chunk: "Retrieved the right policy",
  retrieval_hit_at_1: "Retrieval hit@1",
  retrieval_hit_at_3: "Retrieval hit@3",
  mentions: "Stated the key facts",
  no_forbidden_words: "Made no false claims",
  refund_state: "Left the database correct",
};

export default function QualityPage() {
  const p = quality.primary;
  return (
    <>
      <PageTitle
        lede={`Results of the eval suite: ${p.cases} runs of the hand-labelled cases through the full harness, scored in code, measured on ${measured(p.started_at)}. Below them, a load test.`}
      >
        Quality
      </PageTitle>
      <div className="space-y-14">
        <StatGrid>
          <Stat label="Pass rate" value={`${Math.round((p.passed / p.cases) * 100)}%`} sub={`${p.passed} of ${p.cases} runs`} />
          <Stat label="Mean latency" value={`${p.latency_mean_s.toFixed(1)} s`} sub={`p95 ${p.latency_p95_s.toFixed(1)} s`} />
          <Stat label="Cost per case" value={tinyMoney(p.cost_per_case)} />
          <Stat text label="Model" value={quality.models.find((m) => m.current)?.name ?? "—"} sub={quality.models.find((m) => m.current)?.detail} />
        </StatGrid>

        <div className="grid grid-cols-1 gap-12 lg:grid-cols-2">
          <section>
            <SectionTitle>By category</SectionTitle>
            <Bars rows={Object.entries(p.categories).map(([k, [passed, total]]) => ({ label: CATEGORY[k] ?? k, passed, total }))} />
          </section>
          <section>
            <SectionTitle aside="passed / runs the check applies to">By check</SectionTitle>
            <Bars rows={Object.entries(p.checks).map(([k, [passed, total]]) => ({ label: CHECK[k] ?? k, passed, total }))} />
          </section>
        </div>

        <section>
          <SectionTitle>Models</SectionTitle>
          <TableScroll>
          <table className="w-full text-[13px]">
            <thead className="border-b border-line">
              <tr>
                <Th>Model</Th>
                <Th className="hidden sm:table-cell">Served by</Th>
                <Th align="right">Passed</Th>
                <Th align="right" className="hidden sm:table-cell">Mean latency</Th>
                <Th align="right">Cost per case</Th>
              </tr>
            </thead>
            <tbody>
              {quality.models.map((m) => (
                <tr key={m.name} className="border-b border-line">
                  <Td>
                    <span className="inline-flex items-center gap-2">
                      {m.name}
                      {m.current && (
                        <>
                          <span className="text-muted">·</span>
                          <span className="label !text-[10px] !text-ink">in use</span>
                        </>
                      )}
                    </span>
                  </Td>
                  <Td className="hidden sm:table-cell text-muted">{m.detail}</Td>
                  <Td align="right" className="num">
                    {m.passed}/{m.cases}
                  </Td>
                  <Td align="right" className="hidden sm:table-cell num">{m.latency_mean_s} s</Td>
                  <Td align="right" className="num">{tinyMoney(m.cost_per_case)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
          </TableScroll>
        </section>

        <section>
          <SectionTitle>Load test</SectionTitle>
          <p className="mb-4 max-w-[720px] text-[13px] leading-relaxed text-ink-2">
            Many simulated customers (k6) asking the agent questions at the same time, against a single API process. The
            model is a stand-in that takes 1 s per call, so this measures the harness itself, not the model provider.
            Each question makes 2 model calls, so 2 s is the fastest possible answer.
          </p>
          <TableScroll>
          <table className="w-full text-[13px]">
            <thead className="border-b border-line">
              <tr>
                <Th>Customers at once</Th>
                <Th align="right">Answers / s</Th>
                <Th align="right">p95 answer time</Th>
                <Th align="right" className="hidden sm:table-cell">Errors</Th>
                <Th align="right" className="hidden sm:table-cell">API CPU</Th>
              </tr>
            </thead>
            <tbody>
              {quality.load.map((l) => (
                <tr key={l.users} className="border-b border-line">
                  <Td className="num">{l.users.toLocaleString("en-US")}</Td>
                  <Td align="right" className="num">{l.asks_per_s}</Td>
                  <Td align="right" className="num">{l.p95_s} s</Td>
                  <Td align="right" className="hidden sm:table-cell num">{l.errors}%</Td>
                  <Td align="right" className="hidden sm:table-cell num">{l.cpu}</Td>
                </tr>
              ))}
            </tbody>
          </table>
          </TableScroll>
        </section>
      </div>
    </>
  );
}

function Bars({ rows }: { rows: { label: string; passed: number; total: number }[] }) {
  return (
    <ul>
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[minmax(0,1fr)_72px_48px] items-center gap-4 border-b border-line py-2.5 text-[13px] sm:grid-cols-[minmax(0,1fr)_120px_48px]">
          <span className="truncate">{r.label}</span>
          <span className="h-[3px] bg-line">
            <span className={`block h-full ${r.passed === r.total ? "bg-ink" : "bg-accent"}`} style={{ width: `${(r.passed / r.total) * 100}%` }} />
          </span>
          <span className="num text-right text-muted">
            {r.passed}/{r.total}
          </span>
        </li>
      ))}
    </ul>
  );
}

function measured(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
}
