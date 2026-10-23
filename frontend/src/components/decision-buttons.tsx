"use client";

import { useState, useTransition } from "react";

import { approve, reject } from "@/app/actions";

const QUICK_REASONS = ["Linked to accounts with recent refunds", "Item not returned", "Outside the refund policy"];

export function DecisionButtons({ refundId, amount }: { refundId: number; amount: string }) {
  const [pending, start] = useTransition();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  function run(action: () => ReturnType<typeof approve>) {
    start(async () => {
      setError(null);
      const result = await action();
      if (!result.ok) setError(result.error);
    });
  }

  if (rejecting) {
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(() => reject(refundId, reason));
        }}
      >
        <label className="label mb-2 block" htmlFor={`reason-${refundId}`}>
          Reason for rejecting
        </label>
        <textarea
          id={`reason-${refundId}`}
          autoFocus
          rows={2}
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Recorded in the audit trail"
          className="block w-full resize-none border border-line-strong bg-paper px-3 py-2 text-[13px] leading-snug outline-none placeholder:text-faint focus:border-ink"
        />
        <div className="mt-2 flex flex-wrap gap-1.5">
          {QUICK_REASONS.map((text) => (
            <button
              key={text}
              type="button"
              onClick={() => setReason(text)}
              className="border border-line px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-ink hover:text-ink"
            >
              {text}
            </button>
          ))}
        </div>
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={pending}
            onClick={() => {
              setRejecting(false);
              setError(null);
            }}
            className="border border-line-strong px-4 py-2.5 text-[13px] text-ink-2 transition-colors hover:border-ink disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={pending || reason.trim().length < 3}
            className="flex-1 border border-accent bg-accent px-4 py-2.5 text-left text-[13px] text-paper transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {pending ? "Rejecting…" : "Reject request"}
          </button>
        </div>
        {error && <p className="mt-2 text-[12px] text-accent">{error}</p>}
      </form>
    );
  }

  return (
    <div>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={() => setRejecting(true)}
          className="border border-line-strong px-4 py-2.5 text-[13px] text-ink-2 transition-colors hover:border-accent hover:text-accent disabled:opacity-60"
        >
          Reject
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => run(() => approve(refundId))}
          className="group flex flex-1 items-stretch border border-ink bg-ink text-paper transition-colors hover:bg-ink-2 disabled:opacity-60"
        >
          <span className="flex-1 px-4 py-2.5 text-left text-[13px]">{pending ? "Approving…" : `Approve ${amount}`}</span>
          <span className="flex items-center border-l border-dashed border-paper/30 px-4 transition-transform group-hover:translate-x-0.5">→</span>
        </button>
      </div>
      {error && <p className="mt-2 text-[12px] text-accent">{error}</p>}
    </div>
  );
}
