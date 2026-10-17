"""HTTP layer. The agent logic lives elsewhere; this file only translates HTTP <-> harness."""

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from .adapters.claude_cli import ClaudeCLIClient
from .errors import ModelCallError, ModelTimeoutError, ReplyParseError
from .llm import LLMClient
from .loop import run_agent
from .refund_tools import build_registry
from .schemas import AskRequest, AskResponse
from .tools import ToolRegistry

MAX_STEPS = 5

app = FastAPI(title="Refund agent")


# Dependencies: endpoints ask for these with Depends(...), so tests can swap them out.
def get_llm() -> LLMClient:
    return ClaudeCLIClient()


def get_registry() -> ToolRegistry:
    return build_registry()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/refund-agent/ask")
async def ask_refund_agent(
    req: AskRequest,
    llm: LLMClient = Depends(get_llm),
    tools: ToolRegistry = Depends(get_registry),
) -> AskResponse:
    result = await run_agent(llm, tools, req.question, max_steps=MAX_STEPS)
    return AskResponse(
        answer=result.final_text,
        stop_reason=result.stop_reason,
        num_steps=sum(m.role == "assistant" for m in result.messages),
    )


# Error translation. Messages are fixed on purpose: exception text may contain internals
# (e.g. CLI stderr) and must never reach the client.
def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": message})


@app.exception_handler(ReplyParseError)
async def on_bad_reply(request, exc):
    return _error(502, "The assistant's reply was unusable. Please try again.")


@app.exception_handler(ModelCallError)
async def on_model_error(request, exc):
    return _error(502, "The assistant is unavailable. Please try again.")


@app.exception_handler(ModelTimeoutError)
async def on_model_timeout(request, exc):
    return _error(504, "The assistant took too long. Please try again.")
