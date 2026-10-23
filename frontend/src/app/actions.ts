"use server";

import { refresh } from "next/cache";
import { cookies } from "next/headers";

import { ApiError, api } from "@/lib/api";
import { TENANT_COOKIE, isTenant } from "@/lib/tenants";
import type { AskResponse } from "@/lib/types";

// Server Actions are public endpoints (anyone can POST to them), so every input is checked here, not just in the UI.

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

function failure(error: unknown): { ok: false; error: string } {
  return { ok: false, error: error instanceof ApiError ? error.message : "The harness could not be reached." };
}

export async function setTenant(tenant: string): Promise<void> {
  if (!isTenant(tenant)) return;
  (await cookies()).set(TENANT_COOKIE, tenant, { httpOnly: true, sameSite: "lax", secure: process.env.NODE_ENV === "production", path: "/" });
  refresh();
}

export async function ask(question: string): Promise<Result<AskResponse>> {
  const text = typeof question === "string" ? question.trim() : "";
  if (text.length < 2 || text.length > 1000) return { ok: false, error: "Write between 2 and 1,000 characters." };
  try {
    return { ok: true, data: await api.ask(text) };
  } catch (error) {
    return failure(error);
  }
}

export async function resetDemo(): Promise<Result<null>> {
  try {
    await api.resetDemo();
  } catch (error) {
    return failure(error);
  }
  refresh();
  return { ok: true, data: null };
}

export async function approve(refundId: number): Promise<Result<null>> {
  if (!Number.isInteger(refundId) || refundId <= 0) return { ok: false, error: "Unknown request." };
  try {
    await api.approve(refundId);
  } catch (error) {
    return failure(error);
  }
  refresh();
  return { ok: true, data: null };
}

export async function reject(refundId: number, reason: string): Promise<Result<null>> {
  if (!Number.isInteger(refundId) || refundId <= 0) return { ok: false, error: "Unknown request." };
  const text = typeof reason === "string" ? reason.trim() : "";
  if (text.length < 3 || text.length > 500) return { ok: false, error: "Give a reason (3 to 500 characters)." };
  try {
    await api.reject(refundId, text);
  } catch (error) {
    return failure(error);
  }
  refresh();
  return { ok: true, data: null };
}
