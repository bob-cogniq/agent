FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

# Copy source
COPY src/ src/

# Install project
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "cogniq.main:app", "--host", "0.0.0.0", "--port", "8000"]
