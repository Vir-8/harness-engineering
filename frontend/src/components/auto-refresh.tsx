"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

// Re-renders the page's server data while the tab is visible, so changes made elsewhere (the worker paying an approved
// request, the agent filing a new one) show up without a reload. Pages pass `busy` while something is in flight:
// then it checks every 2 s, otherwise every 15 s (each refresh reaches the harness API, so it should not spin idly).
export function AutoRefresh({ busy = false }: { busy?: boolean }) {
  const every = busy ? 2000 : 15000;
  const router = useRouter();
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, every);
    return () => clearInterval(id);
  }, [router, every]);
  return null;
}
