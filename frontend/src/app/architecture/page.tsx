import type { Metadata } from "next";
import Link from "next/link";

import { Lifecycle } from "@/components/lifecycle";
import { SystemMap } from "@/components/system-map";
import { PageTitle, SectionTitle } from "@/components/ui";
import { STACK } from "@/data/architecture";

export const metadata: Metadata = { title: "Architecture" };

export default function ArchitecturePage() {
  return (
    <>
      <PageTitle lede="Select any part of the diagram to see what it does and where its code is. The blue box is everything the model can reach.">
        Architecture
      </PageTitle>
      <div className="space-y-14">
        <section>
          <SectionTitle>The system</SectionTitle>
          <SystemMap />
        </section>

        <section>
          <SectionTitle>Stack</SectionTitle>
          <dl className="grid grid-cols-1 gap-x-10 sm:grid-cols-2">
            {STACK.map(([layer, items]) => (
              <div key={layer} className="flex flex-col gap-2 border-b border-line py-3 sm:flex-row sm:items-baseline sm:gap-4">
                <dt className="label w-28 shrink-0">{layer}</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {items.map((item) => (
                    <span key={item} className="border border-line bg-surface px-2 py-0.5 text-[12px] text-ink-2">
                      {item}
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </section>

        <section>
          <SectionTitle>The life of a refund</SectionTitle>
          <Lifecycle />
        </section>

        <Link href="/architecture/notes" className="group flex items-center justify-between gap-6 border border-faint bg-surface px-5 py-4 transition-colors hover:border-ink">
          <span>
            <span className="block text-[15px] tracking-[-0.01em] text-ink">Design notes</span>
            <span className="mt-1 block text-[13px] leading-relaxed text-ink-2">
              What can go wrong in a system that pays out money, and what stops each failure.
            </span>
          </span>
          <span className="text-[15px] text-muted transition-transform group-hover:translate-x-0.5 group-hover:text-ink">→</span>
        </Link>

      </div>
    </>
  );
}
