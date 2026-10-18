"""Adapter: Gemini embeddings through the google-genai SDK.

gemini-embedding-2 has no task_type parameter: what the text is FOR (a stored document or a search question)
is expressed by a prefix in the text itself, in the formats Google documents.
"""

import asyncio

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ..embedder import EMBEDDING_DIMENSIONS, Document
from ..errors import ModelCallError, ModelTimeoutError, ReplyParseError

MODEL = "gemini-embedding-2"


def document_prompt(document: Document) -> str:
    return f"title: {document.title or 'none'} | text: {document.text}"


def query_prompt(question: str) -> str:
    return f"task: search result | query: {question}"


class GeminiEmbedder:
    def __init__(
        self,
        client: genai.Client | None = None,  # tests pass a fake; otherwise built on first use from GEMINI_API_KEY
        model: str = MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        timeout: float = 30,
    ):
        self._client = client
        self._model = model
        self.dimensions = dimensions
        self._timeout = timeout

    def _get_client(self) -> genai.Client:
        # Built on first use, so a missing key only breaks calls that really need embeddings, not app startup.
        if self._client is None:
            self._client = genai.Client()
        return self._client

    async def embed_documents(self, documents: list[Document]) -> list[list[float]]:
        if not documents:
            return []
        return await self._embed([document_prompt(d) for d in documents])

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self._embed([query_prompt(text)])
        return vector

    async def _embed(self, prompts: list[str]) -> list[list[float]]:
        """One API call for all `prompts`, one vector back per prompt.

        Each prompt must be its OWN Content object. A plain list of strings is treated as several parts of a single
        Content, and gemini-embedding-2 then merges them into ONE aggregated embedding.
        """
        contents = [types.Content(parts=[types.Part(text=prompt)]) for prompt in prompts]
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._get_client().aio.models.embed_content(
                    model=self._model,
                    contents=contents,
                    config=types.EmbedContentConfig(output_dimensionality=self.dimensions),
                )
        except TimeoutError as e:
            raise ModelTimeoutError("the embeddings call timed out") from e
        except genai_errors.APIError as e:
            # Built from the status only: str(e) contains Google's raw message, which we don't pass on.
            raise ModelCallError(f"the embeddings call failed ({e.code} {e.status})") from e

        vectors = [embedding.values for embedding in (response.embeddings or [])]
        if len(vectors) != len(contents) or any(v is None or len(v) != self.dimensions for v in vectors):
            # Better to stop here than to let the database reject a wrong-sized vector later.
            raise ReplyParseError("the embeddings reply did not have the expected shape")
        return vectors
