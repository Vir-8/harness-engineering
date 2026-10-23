"""What an eval run records: one CaseRun per case (and repeat), gathered into a RunReport that is saved as JSON."""

from pydantic import BaseModel, Field


class ToolRun(BaseModel):
    name: str
    arguments: dict
    output: str
    failed: bool = False  # it crashed, got invalid arguments, or does not exist


class Retrieval(BaseModel):
    """Did plain vector search put the expected policy chunk where it should be? No model call involved except the embedding."""

    rank: int | None  # 1-based position of the expected chunk among the top results; None if it was not there
    distance: float | None  # the expected chunk's cosine distance, when it was among the top results
    best_distance: float | None
    top_titles: list[str]


class JudgeVerdict(BaseModel):
    supported: bool  # every claim in the answer is backed by the tool results
    declines: bool  # the answer says it cannot confirm, instead of answering
    reason: str


class CaseRun(BaseModel):
    case_id: str
    category: str
    merchant: str
    question: str = ""  # reports saved before this field existed have none
    repeat: int = 1
    attempts: int = 1  # more than 1 means the model call failed (infrastructure) and the case was run again
    answer: str = ""
    stop_reason: str | None = None
    error: str | None = None  # the run itself failed (model down, etc.)
    tools: list[ToolRun] = Field(default_factory=list)
    refunds_cents: list[int] = Field(default_factory=list)  # the refund requests in the database afterwards
    retrieval: Retrieval | None = None
    latency_s: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    repeated_calls: int = 0  # tool calls identical to an earlier one in the same run (a retry of the same thing)
    judge: JudgeVerdict | None = None
    judge_error: str | None = None
    judge_cost_usd: float = 0.0
    scores: dict[str, bool | None] = Field(default_factory=dict)  # None = not applicable to this case

    @property
    def passed(self) -> bool:
        return all(value is not False for value in self.scores.values())


class RunReport(BaseModel):
    label: str
    started_at: str
    git_sha: str
    git_dirty: bool
    agent_model: str
    judge_model: str | None
    embedding_model: str
    eval_now: str
    runs: list[CaseRun]
