// The harness API's shapes (src/refund_harness/schemas.py and agent/types.py).

export type RefundStatus = "pending" | "approved" | "rejected" | "issued";

export type LinkStep = { kind: "card" | "address" | "customer"; label: string };

export type LinkedCustomer = { customer_id: string; name: string | null; refunds: number; via: LinkStep[] };

export type RiskFlag = { code: string; message: string; links?: LinkedCustomer[] | null };

// Notes that link this customer to others, as opposed to notes that the check could not run.
export function hasLinkedAccounts(flags: RiskFlag[] | null): boolean {
  return !!flags?.some((flag) => flag.code === "linked_customers" || flag.code === "linked_refunds");
}

export type RefundSummary = {
  refund_id: number;
  order_id: string;
  item: string;
  customer_id: string | null;
  customer_name: string | null;
  amount_cents: number;
  order_total_cents: number;
  status: RefundStatus;
  created_at: string;
  updated_at: string | null;
  risk_flags: RiskFlag[] | null;
  provider_refund_id: string | null;
};

export type ActivityItem = {
  at: string;
  event: "filed" | "approved" | "rejected" | "issued";
  actor: string;
  note: string | null;
  refund_id: number;
  amount_cents: number;
  customer_name: string | null;
  item: string;
  flagged: boolean;
  provider_refund_id: string | null;
};

export type CustomerOrder = {
  order_id: string;
  item: string;
  total_cents: number;
  status: string;
  delivered_at: string | null;
  card_last4: string | null;
  address: string | null;
  refund_id: number | null;
  refund_cents: number | null;
  refund_status: RefundStatus | null;
};

export type CustomerDetail = { customer_id: string; name: string; orders: CustomerOrder[] };

export type RefundEvent = { event: string; actor: string; note: string | null; created_at: string };

export type RefundDetail = RefundSummary & { events: RefundEvent[] };

export type LedgerLine = {
  reference: string;
  state:
    | "matched"
    | "awaiting_payout"
    | "paid_not_recorded"
    | "missing_at_provider"
    | "duplicate_payout"
    | "amount_mismatch"
    | "unknown_payout";
  ok: boolean;
  refund_id: number | null;
  status: string | null;
  amount_cents: number | null;
  provider_refund_ids: string[];
  provider_amount_cents: number;
};

export type Ledger = { lines: LedgerLine[]; available: boolean };

export type QueueDepth = { waiting: number; in_flight: number };

export type OpsSummary = {
  by_status: Partial<Record<RefundStatus, { count: number; amount_cents: number }>>;
  outbox_unsent: number;
  outbox_oldest: string | null;
  payouts_queue: QueueDepth | null;
  dead_letters: QueueDepth | null;
};

export type ToolCall = { id: string; name: string; arguments: Record<string, unknown> };

export type Usage = {
  provider: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number | null;
};

export type Step = {
  kind: "model" | "tool";
  name: string;
  duration_ms: number;
  arguments: Record<string, unknown> | null;
  output: string;
  tool_calls: ToolCall[];
  usage: Usage | null;
  failed: boolean;
};

export type AskResponse = {
  answer: string;
  stop_reason: "final" | "max_steps";
  num_steps: number;
  steps: Step[];
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
};
