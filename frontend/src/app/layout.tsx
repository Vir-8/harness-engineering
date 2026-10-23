import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";

import { DemoReset } from "@/components/demo-reset";
import { MobileNav, Nav } from "@/components/nav";
import { TenantSwitch } from "@/components/tenant-switch";
import { currentTenant } from "@/lib/api";
import { REPO_URL } from "@/lib/repo";

import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Harness", template: "%s · Harness" },
  description: "An agent harness for financial operations.",
};

function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex max-w-[1240px] flex-col gap-4 px-4 py-5 text-[12px] text-muted sm:px-6 md:flex-row md:items-center md:justify-between md:gap-8">
        <span className="max-w-[560px] leading-relaxed">
          Demo data: synthetic customers and orders. Payouts go to a stand-in payment provider, not a real one.
        </span>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <a href={REPO_URL} target="_blank" rel="noreferrer" className="whitespace-nowrap text-ink-2 hover:text-ink">
            Source on GitHub ↗
          </a>
          <DemoReset />
        </div>
      </div>
    </footer>
  );
}

async function Tenant() {
  return <TenantSwitch current={await currentTenant()} />;
}

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} antialiased`}>
      {/* A column as tall as the window, so the footer sits at the bottom even on a short page. */}
      <body className="flex min-h-dvh flex-col">
        <header className="sticky top-0 z-20 border-b border-line bg-paper/90 backdrop-blur-sm">
          {/* From lg up the links sit in this row; below that they are behind the menu button. */}
          <div className="mx-auto flex h-14 max-w-[1240px] items-center gap-x-10 px-4 sm:px-6">
            <Link href="/" className="flex items-baseline gap-2.5">
              <span className="text-[15px] font-semibold tracking-[-0.02em]">Harness</span>
              <span className="label !text-[10px]">Fin Ops</span>
            </Link>
            <Nav />
            <div className="ml-auto flex items-center gap-3">
              <Suspense fallback={<div className="h-8 w-[160px]" />}>
                <Tenant />
              </Suspense>
              <MobileNav />
            </div>
          </div>
        </header>
        <main className="mx-auto w-full max-w-[1240px] flex-1 px-4 pb-20 pt-8 sm:px-6 sm:pb-24 sm:pt-10">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
