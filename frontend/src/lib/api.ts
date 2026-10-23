import "server-only";

import { cookies } from "next/headers";

import { DEFAULT_TENANT, TENANT_COOKIE, isTenant, type TenantId } from "./tenants";
import type { ActivityItem, AskResponse, CustomerDetail, Ledger, OpsSummary, RefundDetail, RefundStatus, RefundSummary } from "./types";

// Server side only: the browser never learns where the API is or its key.
const BASE_URL = process.env.HARNESS_API_URL ?? "http://localhost:8000";
const API_KEY = process.env.HARNESS_API_KEY;
// The console has no sign-in, so every decision made in it is recorded under this one reviewer.
const REVIEWER = "ops@harness.demo";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function currentTenant(): Promise<TenantId> {
  const value = (await cookies()).get(TENANT_COOKIE)?.value;
  return isTenant(value) ? value : DEFAULT_TENANT;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const tenant = await currentTenant();
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-Merchant-Id": tenant,
      "X-Reviewer-Id": REVIEWER,
      ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : `The harness answered ${response.status}.`;
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  refunds: (status?: RefundStatus) => call<RefundSummary[]>(`/refunds${status ? `?status=${status}` : ""}`),
  refund: (id: number) => call<RefundDetail>(`/refunds/${id}`),
  resetDemo: () => call<{ requests: number; unpaid: number }>("/demo/reset", { method: "POST" }),
  activity: (limit = 12) => call<ActivityItem[]>(`/activity?limit=${limit}`),
  customer: (id: string) => call<CustomerDetail>(`/customers/${encodeURIComponent(id)}`),
  ledger: () => call<Ledger>("/ledger"),
  ops: () => call<OpsSummary>("/ops/summary"),
  ask: (question: string) => call<AskResponse>("/refund-agent/ask", { method: "POST", body: JSON.stringify({ question }) }),
  approve: (id: number) => call<{ refund_id: number }>(`/refunds/${id}/approve`, { method: "POST" }),
  reject: (id: number, reason: string) =>
    call<{ refund_id: number }>(`/refunds/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
};
