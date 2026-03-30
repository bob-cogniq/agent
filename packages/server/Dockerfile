FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy workspace root + all packages
COPY pyproject.toml uv.lock ./
COPY packages/shared/ packages/shared/
COPY packages/server/ packages/server/
COPY packages/worker/ packages/worker/

RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "cogniq_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
