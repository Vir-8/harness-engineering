"""Embedders: the offline FakeEmbedder that tests rely on, and GeminiEmbedder against a fake SDK client.

GeminiEmbedder is tested with no network, no API key and no cost. What is checked: the exact text and settings we
send to Google, and how we react when the reply is wrong or the call fails.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from refund_harness.adapters.gemini_embeddings import GeminiEmbedder, document_prompt
from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.retrieval.embedder import (
    EMBEDDING_DIMENSIONS,
    Document,
    FakeEmbedder,
)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # the fake's vectors have length 1, so this is the cosine


async def test_fake_embedder_ranks_shared_words_closer():
    """Every search test relies on this: the fake stands in for meaning with shared words."""
    embedder = FakeEmbedder()
    question = await embedder.embed_query("return window")

    shares_words = await embedder.embed_query("what is the return window")
    shares_nothing = await embedder.embed_query("gift cards never expire")

    assert cosine_similarity(question, shares_words) > cosine_similarity(question, shares_nothing)


# --- GeminiEmbedder ---

class FakeModels:
    """Stands in for client.aio.models: records each call and returns a scripted reply (or raises)."""

    def __init__(self, reply=None, error=None, delay: float = 0):
        self.reply, self.error, self.delay = reply, error, delay
        self.calls: list[dict] = []

    async def embed_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def embedder_with(models: FakeModels, **settings) -> GeminiEmbedder:
    return GeminiEmbedder(client=SimpleNamespace(aio=SimpleNamespace(models=models)), **settings)


def prompts_sent(call: dict) -> list[str]:
    """The text of each Content we sent. Each must hold exactly one part, or Google merges them into one embedding."""
    assert all(len(content.parts) == 1 for content in call["contents"])
    return [content.parts[0].text for content in call["contents"]]


def reply_with(vectors: int, size: int = EMBEDDING_DIMENSIONS) -> types.EmbedContentResponse:
    return types.EmbedContentResponse(embeddings=[types.ContentEmbedding(values=[0.1] * size) for _ in range(vectors)])


# --- what we send ---

@pytest.mark.parametrize(
    "document, prompt",
    [
        (Document("Returns within 30 days.", title="Return window"), "title: Return window | text: Returns within 30 days."),
        (Document("Returns within 30 days."), "title: none | text: Returns within 30 days."),
    ],
    ids=["with_a_title", "without_a_title"],
)
def test_document_prompt_names_its_title(document, prompt):
    assert document_prompt(document) == prompt


async def test_query_sends_model_and_768_dimensions():
    models = FakeModels(reply_with(1))

    await embedder_with(models).embed_query("my headphones broke")

    [call] = models.calls
    assert call["model"] == "gemini-embedding-2"
    assert prompts_sent(call) == ["task: search result | query: my headphones broke"]
    assert call["config"].output_dimensionality == 768


async def test_one_request_per_document():
    """Goes through the REAL SDK (only the network is faked) to see what would actually be sent to Google."""
    sent: dict = {}

    def fake_google(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * EMBEDDING_DIMENSIONS}] * 2})

    client = genai.Client(
        api_key="not-a-real-key",
        http_options=types.HttpOptions(async_client_args={"transport": httpx.MockTransport(fake_google)}),
    )

    vectors = await GeminiEmbedder(client=client).embed_documents([Document("one", title="A"), Document("two", title="B")])

    assert len(vectors) == 2
    assert len(sent["requests"]) == 2  # one embedding request per document, not one merged request
    assert [r["content"]["parts"][0]["text"] for r in sent["requests"]] == [
        "title: A | text: one",
        "title: B | text: two",
    ]


# --- replies we refuse ---

@pytest.mark.parametrize(
    "reply",
    [
        reply_with(1, size=3072),  # the 3072-dimension default: would be rejected later by the vector(768) column
        reply_with(2),  # two vectors back for one question
        reply_with(0),  # none back
        types.EmbedContentResponse(embeddings=None),
    ],
    ids=["wrong_size", "too_many", "none", "missing"],
)
async def test_unexpected_reply_is_refused(reply):
    with pytest.raises(ReplyParseError):
        await embedder_with(FakeModels(reply)).embed_query("my headphones broke")


# --- failures ---

@pytest.mark.parametrize(
    "error",
    [
        genai_errors.ClientError(429, {"error": {"message": "key AIza-secret over quota", "status": "RESOURCE_EXHAUSTED"}}),
        genai_errors.ServerError(503, {"error": {"message": "key AIza-secret overloaded", "status": "UNAVAILABLE"}}),
    ],
    ids=["client_error_429", "server_error_503"],
)
async def test_api_error_hides_googles_message(error):
    with pytest.raises(ModelCallError) as raised:
        await embedder_with(FakeModels(error=error)).embed_query("my headphones broke")

    assert str(error.code) in str(raised.value)
    assert "AIza-secret" not in str(raised.value)


async def test_slow_call_times_out():
    models = FakeModels(reply_with(1), delay=1)

    with pytest.raises(ModelTimeoutError):
        await embedder_with(models, timeout_s=0.01).embed_query("my headphones broke")
