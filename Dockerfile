# One image for every process of the harness. They differ only in the command they start with:
#   API              uvicorn refund_harness.app:app --host 0.0.0.0 --port 8000      (the default below)
#   worker           python scripts/run_worker.py
#   fake provider    uvicorn refund_harness.payments.fake_provider:app --host 0.0.0.0 --port 8100
# docker-compose.yml runs all three (profile "app"). Build: docker build -t refund-harness .

# --- stage 1: the `claude` CLI, which is how the agent reaches the model. It is a Node program. ---
FROM node:22.23.3-bookworm-slim AS claude
# Pinned to the version the adapter was built and evaluated against: a new CLI can change its flags or its JSON.
RUN npm install -g @anthropic-ai/claude-code@2.1.77

# --- stage 2: the app ---
FROM python:3.12.15-slim-bookworm

# Node and the CLI from stage 1; the rest of stage 1 (npm, caches) stays behind.
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first, in their own layer: they change rarely, so a code change rebuilds in seconds, not minutes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# Then our code. --locked: fail if uv.lock is out of date rather than quietly resolve different versions.
COPY src ./src
COPY scripts ./scripts
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Not root: if the app is ever compromised, the attacker is an ordinary user inside the container.
# The CLI keeps its settings under $HOME, so the user needs a home directory.
RUN useradd --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "refund_harness.app:app", "--host", "0.0.0.0", "--port", "8000"]
