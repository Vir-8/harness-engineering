"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { REPO_URL } from "@/lib/repo";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/agent", label: "Agent" },
  { href: "/approvals", label: "Approvals" },
  { href: "/ledger", label: "Ledger" },
  { href: "/quality", label: "Quality" },
  { href: "/architecture", label: "Architecture" },
] as const;

function isActive(path: string, href: string): boolean {
  return href === "/" ? path === "/" : path.startsWith(href);
}

// From lg up: the links in the header row.
export function Nav() {
  const path = usePathname();
  return (
    <nav className="hidden h-full items-stretch gap-6 lg:flex">
      {LINKS.map(({ href, label }) => {
        const active = isActive(path, href);
        return (
          <Link
            key={href}
            href={href}
            className={`relative flex shrink-0 items-center text-[13px] transition-colors ${active ? "text-ink" : "text-muted hover:text-ink"}`}
          >
            {label}
            <span className={`absolute inset-x-0 -bottom-px h-px bg-ink transition-opacity ${active ? "opacity-100" : "opacity-0"}`} />
          </Link>
        );
      })}
    </nav>
  );
}

// Below lg: a menu button that opens the links over the page, under the header. It closes when the page changes, on
// Escape, or with the button; the page behind it does not scroll while it is open. The menu is drawn in document.body:
// the header's backdrop blur makes it the containing block for anything fixed inside it, which would shrink the menu
// to the header's height.
export function MobileNav() {
  const path = usePathname();
  // Open on the page it was opened on, so the menu stays up until the next page has arrived, then closes by itself.
  const [openOn, setOpenOn] = useState<string | null>(null);
  const open = openOn === path;
  const setOpen = (next: boolean) => setOpenOn(next ? path : null);
  const current = LINKS.find((l) => isActive(path, l.href))?.label;

  useEffect(() => {
    if (!open) return;
    const escape = (e: KeyboardEvent) => e.key === "Escape" && setOpenOn(null);
    // Widening the window past lg hides the menu; it must close too, or the scroll lock below would stay on with
    // nothing on screen to release it.
    const wide = window.matchMedia("(min-width: 1024px)");
    const widened = () => wide.matches && setOpenOn(null);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", escape);
    wide.addEventListener("change", widened);
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", escape);
      wide.removeEventListener("change", widened);
    };
  }, [open]);

  return (
    <div className="lg:hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls="mobile-menu"
        aria-label={open ? "Close menu" : "Open menu"}
        className="-mr-2 flex size-10 items-center justify-center text-ink"
      >
        <span className="relative block h-3 w-[18px]">
          <span className={`absolute inset-x-0 h-px bg-current transition-transform duration-200 ${open ? "top-1.5 rotate-45" : "top-0"}`} />
          <span className={`absolute inset-x-0 top-1.5 h-px bg-current transition-opacity duration-200 ${open ? "opacity-0" : ""}`} />
          <span className={`absolute inset-x-0 h-px bg-current transition-transform duration-200 ${open ? "top-1.5 -rotate-45" : "top-3"}`} />
        </span>
      </button>

      {open &&
        createPortal(
          <div id="mobile-menu" className="fixed inset-x-0 bottom-0 top-14 z-30 overflow-y-auto border-t border-line bg-paper lg:hidden">
            <nav className="px-4 pb-8 pt-2 sm:px-6" aria-label={current ? `Pages, on ${current}` : "Pages"}>
              <ul>
                {LINKS.map(({ href, label }, i) => {
                  const active = isActive(path, href);
                  return (
                    <li key={href} className="rise border-b border-line" style={{ animationDelay: `${i * 30}ms` }}>
                      <Link
                        href={href}
                        onClick={() => active && setOpen(false)}
                        aria-current={active ? "page" : undefined}
                        className={`flex items-center justify-between py-4 text-[20px] tracking-[-0.02em] ${active ? "text-ink" : "text-ink-2"}`}
                      >
                        {label}
                        {active && <span className="size-1.5 rounded-full bg-ink" />}
                      </Link>
                    </li>
                  );
                })}
              </ul>
              <a href={REPO_URL} target="_blank" rel="noreferrer" className="mt-6 inline-block text-[13px] text-muted hover:text-ink">
                Source on GitHub ↗
              </a>
            </nav>
          </div>,
          document.body,
        )}
    </div>
  );
}
