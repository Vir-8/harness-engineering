"""HARNESS_API_KEY: when it is set, every route but /health and /metrics needs `Authorization: Bearer <key>`.

A public deployment must set it: the API spends model credits and moves money, and the merchant and reviewer headers are
stand-ins that anyone could send. (conftest unsets it, so every other test runs against an open API.)
"""

from helpers import REVIEWER


async def test_key_required_except_health_and_metrics(client, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "s3cret")

    assert (await client.get("/refunds", headers=REVIEWER)).status_code == 401
    assert (await client.get("/refunds", headers={**REVIEWER, "Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.get("/refunds", headers={**REVIEWER, "Authorization": "Bearer s3cret"})).status_code == 200
    # ...but the load balancer's health check and Prometheus's scrape carry no key
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/metrics")).status_code == 200
