"""Fire N concurrent requests at one path while probing /health every 0.2s.

usage: uv run python scripts/load.py /async-sleep 5
"""

import asyncio
import statistics
import sys
import time

import httpx

BASE = "http://127.0.0.1:8765"


async def timed_get(client: httpx.AsyncClient, path: str) -> float:
    t0 = time.perf_counter()
    response = await client.get(BASE + path)
    response.raise_for_status()  # a fast 404/500 must not look like a fast success
    return time.perf_counter() - t0


async def probe_health(stop: asyncio.Event, latencies: list[float]) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        while not stop.is_set():
            latencies.append(await timed_get(client, "/health"))
            await asyncio.sleep(0.2)


async def main(path: str, n: int) -> None:
    stop, health = asyncio.Event(), []
    probe = asyncio.create_task(probe_health(stop, health))
    await asyncio.sleep(0.5)  # let the probe start before the load

    async with httpx.AsyncClient(timeout=120) as client:
        t0 = time.perf_counter()
        latencies = await asyncio.gather(*[timed_get(client, path) for _ in range(n)])
        wall = time.perf_counter() - t0

    stop.set()
    await probe
    print(f"{path}  x{n}")
    print(f"  total wall time        : {wall:5.2f}s")
    print(f"  request latency  min/med/max: {min(latencies):.2f} / {statistics.median(latencies):.2f} / {max(latencies):.2f}s")
    print(f"  /health latency  median/max : {statistics.median(health):.2f} / {max(health):.2f}s  ({len(health)} probes)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
