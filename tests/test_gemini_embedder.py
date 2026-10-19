"""GeminiEmbedder, tested against a fake SDK client: no network, no API key, no cost.

What is checked: the exact text and settings we send to Google, and how we react when the reply is wrong or the call fails.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from refund_harness.adapters.gemini_embeddings import (
    GeminiEmbedder,
    document_prompt,
    query_prompt,
)
from refund_harness.agent.errors import (
    ModelCallError,
    ModelTimeoutError,
    ReplyParseError,
)
from refund_harness.retrieval.embedder import EMBEDDING_DIMENSIONS, Document


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

def test_a_stored_document_is_labelled_with_its_title():
    assert document_prompt(Document("Returns within 30 days.", title="Return window")) == (
        "title: Return window | text: Returns within 30 days."
    )


def test_a_document_without_a_title_says_none():
    assert document_prompt(Document("Returns within 30 days.")) == "title: none | text: Returns within 30 days."


def test_a_question_is_labelled_as_a_search_query():
    assert query_prompt("my headphones broke") == "task: search result | query: my headphones broke"


async def test_a_question_is_sent_with_the_model_and_the_768_dimension_setting():
    models = FakeModels(reply_with(1))

    await embedder_with(models).embed_query("my headphones broke")

    [call] = models.calls
    assert call["model"] == "gemini-embedding-2"
    assert prompts_sent(call) == ["task: search result | query: my headphones broke"]
    assert call["config"].output_dimensionality == 768


async def test_many_documents_go_in_one_call_and_come_back_in_order():
    models = FakeModels(reply_with(2))
    documents = [Document("Gift cards are final sale.", title="Gift cards"), Document("Orders can be cancelled.")]

    vectors = await embedder_with(models).embed_documents(documents)

    assert len(vectors) == 2
    assert len(models.calls) == 1  # one network round trip, not one per document
    assert prompts_sent(models.calls[0]) == [document_prompt(d) for d in documents]


async def test_no_documents_means_no_call():
    models = FakeModels(reply_with(0))

    assert await embedder_with(models).embed_documents([]) == []
    assert models.calls == []


async def test_the_http_request_holds_one_request_per_document():
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
async def test_an_unexpected_reply_is_refused(reply):
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
async def test_a_failed_call_becomes_a_model_call_error_without_googles_raw_message(error):
    with pytest.raises(ModelCallError) as raised:
        await embedder_with(FakeModels(error=error)).embed_query("my headphones broke")

    assert str(error.code) in str(raised.value)
    assert "AIza-secret" not in str(raised.value)


async def test_a_slow_call_becomes_a_timeout_error():
    models = FakeModels(reply_with(1), delay=1)

    with pytest.raises(ModelTimeoutError):
        await embedder_with(models, timeout=0.01).embed_query("my headphones broke")
