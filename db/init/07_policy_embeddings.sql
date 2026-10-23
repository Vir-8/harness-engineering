-- Each chunk's meaning as a vector, filled in by the ingestion job (src/refund_harness/retrieval/ingest.py).
-- 768 must match EMBEDDING_DIMENSIONS in retrieval/embedder.py. NULL means "not embedded yet".
-- No vector index: each merchant has a handful of chunks, and an exact scan filtered by merchant_id is fast and never
-- misses (an approximate index, filtered per merchant, can return too few rows).
ALTER TABLE policy_chunks ADD COLUMN embedding vector(768);
