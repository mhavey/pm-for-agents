# Use uv official image with Python 3.13
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code from src/ — flattened into /app.
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

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV AWS_DEFAULT_REGION=us-east-1

# Runtime env vars expected at deploy time:
#   DISPUTE_TABLE        required — DynamoDB table for dispute case state
#   AGENT_MODEL_ID       (optional)
#   SYSTEM_PROMPT_PATH   (optional)

CMD ["uv", "run", "opentelemetry-instrument", "python", "dispute_agent.py"]
