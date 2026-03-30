FROM python:3.12-slim

WORKDIR /app

# System deps: git (worktree), Node.js (claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy workspace root + all packages
COPY pyproject.toml uv.lock ./
COPY packages/shared/ packages/shared/
COPY packages/server/ packages/server/
COPY packages/worker/ packages/worker/

RUN uv sync --no-dev

CMD ["uv", "run", "python", "-m", "cogniq_worker.main"]
