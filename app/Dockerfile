# Hivemind app image.
#
# Builds with EITHER the legacy Docker builder OR BuildKit — it uses no
# BuildKit-only features (no `# syntax` directive, no `--mount=type=cache`), so
# `hivemind up` can build it locally on any Docker install, including ones
# without the `buildx` component.
#
# The real embedder uses fastembed (ONNX Runtime, torch-free). When
# INSTALL_EMBEDDINGS=1 the BAAI/bge-small-en-v1.5 model is baked into the image
# at build time so the first query is instant; the model is never loaded at
# import time. The default (0) builds a small fastembed-free image that runs
# with EMBEDDING_MODEL=fake (tests / CI / quick trials).
FROM python:3.11-slim

# FASTEMBED_CACHE_PATH is a normal image path (not a build cache mount) so the
# baked model persists in the image layer and the runtime reads the same dir.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/opt/hivemind/fastembed-cache

WORKDIR /app

ARG INSTALL_EMBEDDINGS=0

# Copy metadata + source, then install. Docker's layer cache reuses this layer
# when neither pyproject.toml/README.md nor the source changed; a version bump
# re-runs it (fastembed's deps are modest and torch-free, so it stays fast).
COPY pyproject.toml README.md ./
COPY hivemind_app ./hivemind_app
RUN pip install --upgrade pip && \
    if [ "$INSTALL_EMBEDDINGS" = "1" ]; then \
        pip install ".[embeddings]"; \
    else \
        pip install .; \
    fi

# Bake the ONNX model into the image (only when the real embedder is installed)
# so the first runtime query pays no download cost.
RUN if [ "$INSTALL_EMBEDDINGS" = "1" ]; then \
        python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"; \
    fi

# MCP server listens on MCP_PORT (default 7777); the CLI wires env + ports.
EXPOSE 7777

CMD ["python", "-m", "hivemind_app"]
