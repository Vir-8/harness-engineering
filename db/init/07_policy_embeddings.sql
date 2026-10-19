-- Each chunk's meaning as a vector, filled in by the ingestion job (src/refund_harness/retrieval/ingest.py).
-- 768 must match EMBEDDING_DIMENSIONS in retrieval/embedder.py. NULL means "not embedded yet".
-- No index yet on purpose: we compare search with and without one later.
ALTER TABLE policy_chunks ADD COLUMN embedding vector(768);
