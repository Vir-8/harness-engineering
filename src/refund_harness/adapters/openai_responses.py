"""Adapter: OpenAI's Responses API, with the model's own (native) tool calling.

Translates our provider-neutral Messages to Responses "input items" and back:

    our Message                          Responses input item
    user                            ->   {"role": "user", "content": ...}
    assistant (text, tool_calls)    ->   the items OpenAI returned for that turn, replayed verbatim (provider_items):
                                         reasoning (encrypted), message, function_call
    tool (result of one call)       ->   {"type": "function_call_output", "call_id": ..., "output": ...}

Stateless on purpose (store=False): OpenAI keeps nothing between calls, and every call carries the whole conversation,
exactly like the rest of the harness. The model is a reasoning model, and its reasoning from earlier turns is passed back
ENCRYPTED (include=["reasoning.encrypted_content"]), so it can continue its thinking after a tool result instead of
starting over, without OpenAI storing the conversation.

Reads OPENAI_API_KEY. The SDK retries rate limits (429), server errors (5xx) and dropped connections itself, with
backoff; what still fails is raised as our own ModelError types, like every other adapter.
"""

import json
from json import JSONDecodeError
from typing import Any

import openai
from openai import AsyncOpenAI

from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.agent.pricing import estimate_cost
from refund_harness.agent.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

# The cheapest current OpenAI model (US$0.10 / 0.50 per million tokens in / out, checked 2026-10-23), made for "focused,
# high-volume tasks". Pinned for the same reason as the Claude adapter: evals compare like with like.
DEFAULT_MODEL = "gpt-6-luna"
# How hard it thinks before answering: none | low | medium (OpenAI's default) | high | xhigh | max. Picking a tool and
# reading a policy needs little; "medium" roughly triples the time to the first token for this kind of task.
DEFAULT_REASONING_EFFORT = "low"

# The Claude adapter gets its framing from Claude Code's own system prompt; this API has none, so we give one.
# It says what the job is, to rely on the tools, and not to promise what the tools cannot do; rules about a specific
# action live in that tool's description, where every provider sees them.
INSTRUCTIONS = (
    "You are the customer support agent of an online shop, helping a customer with their order or a refund. "
    "Use the tools to look up the order and the shop's policies instead of guessing, and base your answer on what they "
    "return. For any question about refunds or returns, search the policy before replying, even when you also need "
    "more details from the customer. You can look up orders, search the policy and file refund requests, nothing "
    "else: never offer or promise anything your tools cannot do, such as store credit, a repair, a replacement or an "
    "exchange. When the policy mentions an option like that, say the shop's team handles it. "
    "Reply to the customer in plain, friendly language."
)

# A ceiling for a runaway reply, NOT a length the model aims for: it is not told the limit, and only the tokens it really
# produces are billed. The largest whole eval case used 241 output tokens; the headroom is for higher reasoning efforts,
# where the thinking alone can run to thousands. At this ceiling a runaway costs at most US$0.008 on gpt-6-luna.
MAX_OUTPUT_TOKENS = 16_000


class OpenAIClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        *,
        client: AsyncOpenAI | None = None,
        timeout_s: float = 60,
        max_retries: int = 2,
        instructions: str = INSTRUCTIONS,
    ):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.instructions = instructions
        self._client = client
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    def _get_client(self) -> AsyncOpenAI:
        # Built on first use, so a missing key only breaks calls that need the model, not app startup.
        if self._client is None:
            self._client = AsyncOpenAI(timeout=self._timeout_s, max_retries=self._max_retries)
        return self._client

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        response = await self._create(input=to_input_items(messages), tools=[to_function_tool(t) for t in tools])
        return self.parse_response(response)

    async def complete_structured(self, prompt: str, schema: dict) -> tuple[dict, Usage | None]:
        """One prompt in, one JSON object out (matching `schema`), plus what it cost. Same contract as the Claude adapter."""
        response = await self._create(
            input=[{"role": "user", "content": prompt}],
            text={"format": {"type": "json_schema", "name": "reply", "schema": schema, "strict": False}},
        )
        text, _ = _read_output(response)
        try:
            structured = json.loads(text)
            if not isinstance(structured, dict):
                raise TypeError("not a JSON object")
        except (JSONDecodeError, TypeError) as e:
            raise ReplyParseError("The model's reply was unusable") from e
        return structured, self.parse_usage(response)

    async def _create(self, **request: Any):
        try:
            client = self._get_client()
            response = await client.responses.create(
                model=self.model,
                instructions=self.instructions,
                reasoning={"effort": self.reasoning_effort},
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
                include=["reasoning.encrypted_content"],
                **request,
            )
        except openai.APITimeoutError as e:  # before APIConnectionError: it is a subclass
            raise ModelTimeoutError(f"the OpenAI call gave no answer within {self._timeout_s:g} s") from e
        except openai.APIStatusError as e:  # what is left after the SDK's own retries: 4xx, or 5xx/429 that kept failing
            raise ModelCallError(f"OpenAI answered {e.status_code} ({type(e).__name__}): {e.message[:300]}") from e
        except openai.OpenAIError as e:  # no API key, connection refused...
            raise ModelCallError(f"OpenAI call failed ({type(e).__name__}): {str(e)[:300]}") from e
        if response.status == "incomplete":
            # Cut off (the token ceiling, a content filter): any text is a fragment, and any tool call may be part of
            # an unfinished plan. A clear failure the caller can retry beats a half-sentence sent to a customer.
            reason = response.incomplete_details.reason if response.incomplete_details else "unknown"
            raise ModelCallError(f"OpenAI stopped before finishing its reply (incomplete: {reason})")
        return response

    def parse_response(self, response) -> LLMResponse:
        text, tool_calls = _read_output(response)
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            usage=self.parse_usage(response),
            provider_items=[item.model_dump(exclude_none=True) for item in response.output],
        )

    def parse_usage(self, response) -> Usage | None:
        """OpenAI's input_tokens is already the total; cached reads and cache writes are parts of it, as in our Usage."""
        counts = response.usage
        if counts is None:
            return None
        details = counts.input_tokens_details
        usage = Usage(
            provider="openai",
            request_model=self.model,
            model=response.model,
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,  # includes the reasoning tokens, which are billed as output
            cache_read_tokens=details.cached_tokens if details else 0,
            cache_write_tokens=details.cache_write_tokens if details else 0,  # 30-minute writes, OpenAI's default
        )
        return usage.model_copy(update={"cost_usd": estimate_cost(usage)})


def to_function_tool(spec: ToolSpec) -> dict:
    # strict=False: strict mode rejects some JSON Schema keywords our tools use (minLength...). Arguments are validated
    # by the tool registry (Pydantic) either way, and a bad one goes back to the model as an error it can fix.
    return {"type": "function", "name": spec.name, "description": spec.description, "parameters": spec.parameters, "strict": False}


def to_input_items(messages: list[Message]) -> list[dict]:
    items: list[dict] = []
    for m in messages:
        if m.role == "user":
            items.append({"role": "user", "content": m.content})
        elif m.role == "tool":
            items.append({"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content})
        elif m.provider_items is not None:
            items.extend(m.provider_items)  # this adapter's own record of the turn, reasoning included
        else:
            # An assistant turn that did not come from this adapter (a test, another provider): rebuild it.
            if m.content:
                items.append({"role": "assistant", "content": m.content})
            for call in m.tool_calls:
                items.append({"type": "function_call", "call_id": call.id, "name": call.name, "arguments": json.dumps(call.arguments)})
    return items


def _function_calls(response) -> list:
    return [item for item in response.output if item.type == "function_call"]


def _read_output(response) -> tuple[str, list[ToolCall]]:
    """The reply's text (a refusal counts as text: it is the model's answer) and its tool calls."""
    texts = []
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                texts.append(part.text if part.type == "output_text" else getattr(part, "refusal", ""))
    try:
        calls = [ToolCall(id=c.call_id, name=c.name, arguments=json.loads(c.arguments or "{}")) for c in _function_calls(response)]
    except (JSONDecodeError, TypeError, ValueError) as e:
        raise ReplyParseError("The model's tool call had unreadable arguments") from e
    return "".join(texts), calls
