"""A stand-in for a payment provider (think Stripe): it "sends money back" and keeps a ledger of every payout.

It runs as its OWN process, like the real thing, so it keeps its ledger when our code crashes:

    uv run uvicorn refund_harness.payments.fake_provider:app --port 8100

The ledger lives in memory, so restarting the provider empties it. GET /v1/refunds shows what was paid.

Idempotency, the way real payment APIs do it: a request may carry an `Idempotency-Key` header. The first request with
a key pays and the provider remembers the key with its result. A later request with the same key does NOT pay again: it
gets the remembered result back (marked by the `Idempotent-Replayed: true` header). The same key with a different body
is refused, because it means the caller's keys are broken. A request without a key pays every time.
"""

from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field


class RefundIn(BaseModel):
    amount_cents: int = Field(gt=0)
    reference: str = Field(min_length=1)  # the caller's own label, e.g. "acme:refund-7"; the provider does not check it


class RefundOut(BaseModel):
    id: str
    amount_cents: int
    reference: str
    status: str
    created_at: datetime


def create_app() -> FastAPI:
    """A provider with an empty ledger (tests make a fresh one each)."""
    provider = FastAPI(title="Fake payment provider")
    ledger: list[RefundOut] = []
    seen: dict[str, tuple[RefundIn, RefundOut]] = {}  # idempotency key -> (the request it came with, its result)

    @provider.post("/v1/refunds")
    async def create_refund(
        body: RefundIn, response: Response, idempotency_key: str | None = Header(default=None, max_length=255)
    ) -> RefundOut:
        # Check-then-store with no `await` in between: one event loop runs this without interruption, so two requests
        # with the same key cannot both get past the check. A real provider needs a unique constraint or a lock here.
        if idempotency_key is not None and idempotency_key in seen:
            first_request, first_result = seen[idempotency_key]
            if body != first_request:
                raise HTTPException(409, "This idempotency key was already used with a different request.")
            response.headers["Idempotent-Replayed"] = "true"
            return first_result

        refund = RefundOut(
            id=f"re_{len(ledger) + 1:06d}",
            amount_cents=body.amount_cents,
            reference=body.reference,
            status="succeeded",
            created_at=datetime.now(UTC),
        )
        ledger.append(refund)
        if idempotency_key is not None:
            seen[idempotency_key] = (body, refund)
        return refund

    @provider.delete("/v1/refunds", status_code=204)
    async def clear_ledger() -> None:
        """Empty the ledger and forget every key: a stand-in's convenience, like a test-mode reset (used by the demo reset)."""
        ledger.clear()
        seen.clear()

    @provider.get("/v1/refunds")
    async def list_refunds(reference: str | None = None) -> list[RefundOut]:
        return [r for r in ledger if reference is None or r.reference == reference]

    return provider


app = create_app()
