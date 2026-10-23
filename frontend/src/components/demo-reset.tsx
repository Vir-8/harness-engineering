"use client";

import { useState, useTransition } from "react";

import { resetDemo } from "@/app/actions";

// Two clicks, because a reset rewrites what every visitor sees.
export function DemoReset() {
  const [confirming, setConfirming] = useState(false);
  const [pending, start] = useTransition();
  const [message, setMessage] = useState<string | null>(null);

  function reset() {
    start(async () => {
      const result = await resetDemo();
      setConfirming(false);
      setMessage(result.ok ? "The demo data was reset." : result.error);
    });
  }

  return (
    <span className="inline-flex items-center gap-3">
      {message && !pending && <span className="text-muted">{message}</span>}
      {confirming ? (
        <>
          <span className="text-ink-2">Reset everything for both merchants?</span>
          <button type="button" disabled={pending} onClick={() => setConfirming(false)} className="text-muted hover:text-ink disabled:opacity-60">
            Cancel
          </button>
          <button type="button" disabled={pending} onClick={reset} className="border border-ink px-2.5 py-1 text-ink hover:bg-ink hover:text-paper disabled:opacity-60">
            {pending ? "Resetting…" : "Reset"}
          </button>
        </>
      ) : (
        <button
          type="button"
          onClick={() => {
            setMessage(null);
            setConfirming(true);
          }}
          className="border border-line-strong px-2.5 py-1 text-ink-2 hover:border-ink hover:text-ink"
        >
          Reset demo data
        </button>
      )}
    </span>
  );
}
