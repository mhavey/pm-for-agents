# Use uv official image with Python 3.13
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Enable bytecode compilation for faster startup in production
ENV UV_COMPILE_BYTECODE=1

# Copy dependency manifest + lockfile.
# Run `uv lock` locally before building to (re)generate uv.lock.
COPY pyproject.toml uv.lock ./

# Install dependencies into /app/.venv (no dev deps).
RUN uv sync --frozen --no-dev

# Copy application code from src/ — flattened into /app so research_agent.py
# and the prompts/ directory sit alongside (the agent loads each prompt via
# Path(__file__).parent / "prompts" / ...).
COPY src/ ./

# AgentCore default port
EXPOSE 8080

# --- Observability Configuration ---
ENV OTEL_PYTHON_DISTRO="aws_distro"
ENV OTEL_PYTHON_CONFIGURATOR="aws_configurator"
ENV OTEL_TRACES_EXPORTER="otlp"
ENV OTEL_LOGS_EXPORTER="otlp"
ENV OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
ENV AGENT_OBSERVABILITY_ENABLED="true"
ENV OTEL_PYTHON_ID_GENERATOR="xray"
ENV OTEL_PROPAGATORS="xray,tracecontext,baggage"
ENV OTEL_AWS_ID_GENERATOR="true"
ENV OTEL_METRICS_EXPORTER="none"

# Standard Environment Variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV AWS_DEFAULT_REGION=us-east-1

# Runtime env vars expected at deploy time:
#   KNOWLEDGE_BASE_ID, MEMORY_ID, OPENSEARCH_HOST,
#   AGENT_MODEL_ID (optional), ANALYZE_MODEL_ID (optional),
#   OPENSEARCH_INDEX (optional), MIN_SCORE (optional)

CMD ["uv", "run", "opentelemetry-instrument", "python", "research_agent.py"]
