"""The LLM judge: a second model call that grades the agent's final answer against the evidence the agent had.

It answers two questions code cannot: is every claim in the answer backed by the tool results (evidence support), and
did the answer decline instead of answering (abstention). Judges make mistakes, so verdicts are meant to be checked
against human labels before they are trusted.
"""

from collections.abc import Awaitable, Callable

from refund_harness.adapters.claude_cli import ClaudeCLIClient
from refund_harness.agent.types import ToolSpec, Usage
from refund_harness.evals.results import JudgeVerdict, ToolRun

# an `ask` takes a prompt and a JSON schema and returns the model's object plus what the call cost
Ask = Callable[[str, dict], Awaitable[tuple[dict, Usage | None]]]

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "declines": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["supported", "declines", "reason"],
}


RUBRIC = """Decide two things.
1. supported: true if every factual claim the answer makes about this shop's policies, orders or refunds is stated in the
   evidence. A claim the evidence does not contain (a policy, a number, a deadline, a status) makes this false, even if it
   sounds reasonable. Polite phrases, apologies and offers to help are not claims. An answer that makes no claims at all
   is supported.
2. declines: true if the answer says it cannot confirm, does not know, or cannot help with the question, instead of
   answering it.
Give a one-sentence reason."""


def format_evidence(tools: list[ToolRun]) -> str:
    if not tools:
        return "(the agent used no tools, so it had no evidence at all)"
    return "\n\n".join(f"Tool {t.name}({t.arguments}) returned:\n{t.output}" for t in tools)


def format_capabilities(specs: list[ToolSpec]) -> str:
    return "\n".join(f"- {spec.name}: {spec.description}" for spec in specs) or "(none)"


def build_judge_prompt(question: str, tools: list[ToolRun], answer: str, specs: list[ToolSpec] | None = None) -> str:
    return f"""You are grading an AI agent that answers customers of an online shop.
Judge ONLY against the evidence below. Do not use your own knowledge of what shops usually do.

Customer's question:
{question}

Evidence the agent was given (the results of its tool calls):
{format_evidence(tools)}

How the agent works (the descriptions of the tools it was given). A claim about what the agent itself can or cannot do,
such as what a tool does or that a refund request waits for human approval, is supported if these descriptions say so:
{format_capabilities(specs or [])}

The agent's final answer:
{answer}

{RUBRIC}"""


class Judge:
    def __init__(self, ask: Ask, model: str):
        self._ask = ask
        self.model = model

    @classmethod
    def with_claude(cls, model: str) -> "Judge":
        return cls(ClaudeCLIClient(model=model).complete_structured, model)

    async def grade(
        self, question: str, tools: list[ToolRun], answer: str, specs: list[ToolSpec] | None = None
    ) -> tuple[JudgeVerdict, Usage | None]:
        verdict, usage = await self._ask(build_judge_prompt(question, tools, answer, specs), JUDGE_SCHEMA)
        return JudgeVerdict(**verdict), usage
