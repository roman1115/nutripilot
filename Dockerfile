FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

ARG INSTALL_ML=false
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
COPY apps ./apps
COPY data ./data
COPY evals ./evals
COPY migrations ./migrations
RUN pip install --upgrade pip uv==0.11.32 \
    && if [ "$INSTALL_ML" = "true" ]; then \
         uv sync --frozen --no-dev --no-editable --extra ml; \
       else \
         uv sync --frozen --no-dev --no-editable; \
       fi \
    && useradd --create-home --uid 10001 nutripilot \
    && chown -R nutripilot:nutripilot /app

USER nutripilot

EXPOSE 8000 8010
CMD ["uvicorn", "nutripilot.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
