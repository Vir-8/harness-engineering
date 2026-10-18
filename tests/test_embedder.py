"""FakeEmbedder: the offline stand-in used wherever tests need embeddings."""

import math

import pytest

from refund_harness.embedder import EMBEDDING_DIMENSIONS, Document, FakeEmbedder


def cosine_similarity(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # the fake's vectors have length 1, so this is the cosine


async def test_vectors_have_the_size_the_database_column_expects():
    vector = await FakeEmbedder().embed_query("damaged item")

    assert len(vector) == EMBEDDING_DIMENSIONS == 768


async def test_the_same_text_always_gives_the_same_vector():
    embedder = FakeEmbedder()

    assert await embedder.embed_query("damaged item") == await embedder.embed_query("damaged item")


async def test_vectors_have_length_one():
    vector = await FakeEmbedder().embed_query("items may be returned within 30 days")

    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0)


async def test_texts_that_share_words_are_closer_than_texts_that_do_not():
    embedder = FakeEmbedder()
    question = await embedder.embed_query("return window")

    shares_words = await embedder.embed_query("what is the return window")
    shares_nothing = await embedder.embed_query("gift cards never expire")

    assert cosine_similarity(question, shares_words) > cosine_similarity(question, shares_nothing)


async def test_documents_come_back_one_vector_per_text_in_order():
    embedder = FakeEmbedder()

    vectors = await embedder.embed_documents([Document("gift cards"), Document("return window")])

    assert vectors == [await embedder.embed_query("gift cards"), await embedder.embed_query("return window")]


async def test_a_documents_title_is_part_of_what_gets_embedded():
    embedder = FakeEmbedder()

    [with_title] = await embedder.embed_documents([Document("policy text", title="Gift cards")])
    [without_title] = await embedder.embed_documents([Document("policy text")])

    assert with_title != without_title


@pytest.mark.parametrize("blank", ["", "   ", "!!!"], ids=["empty", "spaces", "punctuation_only"])
async def test_text_with_no_words_is_refused(blank):
    with pytest.raises(ValueError):
        await FakeEmbedder().embed_query(blank)
