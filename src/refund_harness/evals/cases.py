"""The eval cases: what to ask, and what a good run looks like. Loaded and validated from evals/cases.yaml."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CASES_FILE = Path(__file__).resolve().parents[3] / "evals" / "cases.yaml"
KNOWN_TOOLS = {"get_order", "search_policy", "submit_order_refund"}
KNOWN_MERCHANTS = {"acme", "globex"}
CATEGORIES = {"policy", "paraphrase", "order", "refund", "tenant", "abstain", "safety", "multi"}


class EvalCase(BaseModel):
    id: str = Field(min_length=3)
    merchant: str
    category: str
    question: str = Field(min_length=3)
    tools_called: list[str] = []
    tools_not_called: list[str] = []
    expected_chunk: str | None = None
    must_mention: list[str] = []
    must_not_mention: list[str] = []
    refund_created: bool | None = None
    refund_amount_cents: int | None = Field(default=None, gt=0)
    should_abstain: bool = False
    notes: str | None = None

    model_config = {"extra": "forbid"}  # a typo in a field name must fail loudly, not silently check nothing

    @field_validator("merchant")
    @classmethod
    def merchant_is_known(cls, value: str) -> str:
        if value not in KNOWN_MERCHANTS:
            raise ValueError(f"unknown merchant {value!r}, expected one of {sorted(KNOWN_MERCHANTS)}")
        return value

    @field_validator("category")
    @classmethod
    def category_is_known(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"unknown category {value!r}, expected one of {sorted(CATEGORIES)}")
        return value

    @field_validator("tools_called", "tools_not_called")
    @classmethod
    def tools_exist(cls, tools: list[str]) -> list[str]:
        unknown = set(tools) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools {sorted(unknown)}, expected some of {sorted(KNOWN_TOOLS)}")
        return tools

    @field_validator("must_mention", "must_not_mention")
    @classmethod
    def words_are_not_blank(cls, words: list[str]) -> list[str]:
        if any(not word.strip() or any(not part.strip() for part in word.split("|")) for word in words):
            raise ValueError("an empty word (or an empty alternative like 'a||b') would match every answer")
        return words

    @model_validator(mode="after")
    def labels_do_not_contradict(self) -> "EvalCase":
        if set(self.tools_called) & set(self.tools_not_called):
            raise ValueError("a tool cannot be both required and forbidden")
        if self.refund_amount_cents is not None and self.refund_created is not True:
            raise ValueError("refund_amount_cents only makes sense when refund_created is true")
        if self.should_abstain and self.refund_created:
            raise ValueError("a case that should abstain cannot also expect a refund")
        return self


def load_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text())
    cases = [EvalCase(**item) for item in raw["cases"]]
    ids = [case.id for case in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return cases
