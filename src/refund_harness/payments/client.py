"""Our side of the payment provider: one place that knows where it is and how to ask it to send money back."""

import os
from dataclasses import dataclass

import httpx

DEFAULT_URL = "http://localhost:8100"  # the fake provider, run locally
DEFAULT_TIMEOUT_S = 10.0


class PaymentError(Exception):
    """The provider refused, failed, or could not be reached. The money may or may not have moved."""


@dataclass(frozen=True)
class ProviderRefund:
    id: str
    amount_cents: int
    reference: str
    replayed: bool = False  # True: the provider had already done this (same idempotency key) and sent nothing new


class PaymentProvider:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    @classmethod
    def from_env(cls) -> "PaymentProvider":
        url = os.environ.get("PAYMENTS_URL", DEFAULT_URL)
        return cls(httpx.AsyncClient(base_url=url, timeout=DEFAULT_TIMEOUT_S))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def clear_ledger(self) -> None:
        """Empty the stand-in provider's ledger. Only the demo reset calls this; a real provider has no such thing."""
        await self._send("DELETE", "/v1/refunds")

    async def list_refunds(self) -> list[ProviderRefund]:
        """Every payout on the provider's ledger (a real provider would page through them, and filter by date)."""
        response = await self._send("GET", "/v1/refunds")
        return [ProviderRefund(id=r["id"], amount_cents=r["amount_cents"], reference=r["reference"]) for r in response.json()]

    async def create_refund(self, amount_cents: int, reference: str, idempotency_key: str) -> ProviderRefund:
        """Send money back to the customer, at most once per `idempotency_key`.

        The key must name the payout, not the attempt: the same on every retry of one refund, different for every refund.
        Then a retry after a crash or a timeout gets the first payout back instead of making a second one.
        """
        # A timeout leaves us not knowing whether the money moved. With the key, simply retrying is safe.
        response = await self._send(
            "POST",
            "/v1/refunds",
            json={"amount_cents": amount_cents, "reference": reference},
            headers={"Idempotency-Key": idempotency_key},
        )
        body = response.json()
        return ProviderRefund(
            id=body["id"],
            amount_cents=body["amount_cents"],
            reference=body["reference"],
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    async def _send(self, method: str, path: str, **request) -> httpx.Response:
        """Every way a call can go wrong (refused, failed, unreachable, timed out) comes out as one PaymentError."""
        try:
            response = await self._http.request(method, path, **request)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise PaymentError(f"{type(error).__name__}: {error}") from error
        return response
