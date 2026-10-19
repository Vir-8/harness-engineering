"""Throwaway server for the blocking experiment. Not part of the real app.

The fake model never asks for a tool, so the harness runs here with an empty tool registry and no database.
"""

import asyncio
import time

from fastapi import FastAPI

from refund_harness.agent.llm import LLMResponse
from refund_harness.agent.loop import run_agent
from refund_harness.agent.tools import ToolRegistry

app = FastAPI()
WORK_SECONDS = 2

class FakeTimeLLM:
    def __init__(self, is_async: bool):
        self.is_async = is_async
    async def complete(self, messages, tools):
        if self.is_async:
            await asyncio.sleep(2)
        else:
            time.sleep(2)
        return LLMResponse(text="ok")

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/async-sleep")
async def async_sleep():
    await asyncio.sleep(WORK_SECONDS)  # waiting the event loop can watch
    return {"done": True}


@app.get("/blocking-sleep")
async def blocking_sleep():
    time.sleep(WORK_SECONDS)  # waiting the event loop CANNOT watch
    return {"done": True}


@app.get("/sync-def-sleep")
def sync_def_sleep():  # note: plain `def`, not `async def`
    time.sleep(WORK_SECONDS)
    return {"done": True}


@app.get("/harness-test-asyncio")
async def harness_test_asyncio():
    llm = FakeTimeLLM(True)
    await run_agent(llm, ToolRegistry([]), "what?")
    return {"done": True}


@app.get("/harness-test-time")
async def harness_test_time():
    llm = FakeTimeLLM(False)
    await run_agent(llm, ToolRegistry([]), "what?")
    return {"done": True}