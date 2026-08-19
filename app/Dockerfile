# Hivemind app image.
#
# The embedding model (BAAI/bge-small-en-v1.5) is downloaded on first use at
# runtime, not baked into the image, and never at import time.
FROM python:3.11-slim

# System deps kept minimal; sentence-transformers pulls torch as a wheel.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package (deps resolved from pyproject) — copy metadata first for
# better layer caching, then the source.
COPY pyproject.toml README.md ./
COPY hivemind_app ./hivemind_app

# INSTALL_EMBEDDINGS=1 pulls the heavy real embedder (sentence-transformers ->
# torch) for production. The default (0) builds a small, torch-free image for
# tests/CI, which run with EMBEDDING_MODEL=fake.
ARG INSTALL_EMBEDDINGS=0
RUN pip install --upgrade pip && \
    if [ "$INSTALL_EMBEDDINGS" = "1" ]; then pip install ".[embeddings]"; else pip install .; fi

# MCP server listens on MCP_PORT (default 7777); the CLI wires env + ports.
EXPOSE 7777

CMD ["python", "-m", "hivemind_app"]
