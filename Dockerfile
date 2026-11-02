# syntax=docker/dockerfile:1.7
# One file, one image per role. The environment comes only from pyproject.toml
# and uv.lock:   docker build --target serve -t rec-serve .
ARG PYTHON=3.12

# ---- builder: resolve and install into /opt/venv -----------------------------
FROM python:${PYTHON}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# ---- serve: numpy + fastapi only (no scipy, no mlflow) ------------------------
FROM builder AS serve-build
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra serve

FROM python:${PYTHON}-slim AS serve
RUN useradd --system --uid 10001 rec
COPY --from=serve-build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 RECSYS_LOG_JSON=true
USER 10001
EXPOSE 8080
# Scale with processes/pods, not threads: the engine holds the GIL.
ENTRYPOINT ["rec-serve", "--port", "8080"]

# ---- train: scipy + optional MLflow client ------------------------------------
FROM builder AS train-build
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra train --extra tracking

FROM python:${PYTHON}-slim AS train
RUN useradd --system --uid 10001 rec
COPY --from=train-build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 RECSYS_LOG_JSON=true
USER 10001
ENTRYPOINT ["rec-train"]

# ---- monitor: rec-loop (drift check, rollout steps). Needs scipy because a
# retrain can be triggered from it. ---------------------------------------------
FROM python:${PYTHON}-slim AS monitor
RUN useradd --system --uid 10001 rec
COPY --from=train-build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 RECSYS_LOG_JSON=true
USER 10001
ENTRYPOINT ["rec-loop"]
