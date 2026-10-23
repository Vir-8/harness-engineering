import type { Metadata } from "next";
import Link from "next/link";

import { NotesTable } from "@/components/notes-table";
import { PageTitle } from "@/components/ui";
import { FAILURES } from "@/data/architecture";
import { sourceUrl } from "@/lib/repo";

export const metadata: Metadata = { title: "Design notes" };

// Linked from the Architecture page rather than the nav: the detail behind the diagrams, for whoever wants it.
export default function DesignNotesPage() {
  return (
    <>
      <Link href="/architecture" className="mb-6 inline-block text-[13px] text-muted hover:text-ink">
        ← Architecture
      </Link>
      <PageTitle
        lede="What can go wrong in a system that pays out money, and what stops each failure. Every guard below has a test that makes the failure happen."
        aside={
          <a href={sourceUrl("tests/")} target="_blank" rel="noreferrer" className="text-[13px] text-ink-2 hover:text-ink">
            The tests ↗
          </a>
        }
      >
        Design notes
      </PageTitle>
      <div className="space-y-14">
        <section>
          <NotesTable
            columns={["What could go wrong", "What stops it"]}
            widths="md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]"
            rows={FAILURES.map((f) => ({
              key: f.failure,
              cells: [
                <span key="f" className="text-ink">{f.failure}</span>,
                <span key="g" className="text-ink-2">{f.guard}</span>,
              ],
            }))}
          />
        </section>
      </div>
    </>
  );
}
