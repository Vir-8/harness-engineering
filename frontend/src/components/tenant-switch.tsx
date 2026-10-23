"use client";

import { useTransition } from "react";

import { setTenant } from "@/app/actions";
import { TENANTS, type TenantId } from "@/lib/tenants";

export function TenantSwitch({ current }: { current: TenantId }) {
  const [pending, start] = useTransition();
  return (
    <label
      title="Each merchant is a separate tenant: every query, tool and payout is scoped to the one selected here."
      className={`flex items-center gap-2.5 transition-opacity ${pending ? "opacity-60" : ""}`}
    >
      <span className="label hidden !text-[10px] sm:inline">Merchant</span>
      <span className="relative">
        <select
          value={current}
          disabled={pending}
          onChange={(e) => start(() => setTenant(e.target.value))}
          className="cursor-pointer appearance-none border border-line-strong bg-surface py-1 pl-2.5 pr-7 text-[12px] outline-none hover:border-ink focus-visible:border-ink"
        >
          {TENANTS.map((tenant) => (
            <option key={tenant.id} value={tenant.id}>
              {tenant.name}
            </option>
          ))}
        </select>
        <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[9px] text-muted">▼</span>
      </span>
    </label>
  );
}
