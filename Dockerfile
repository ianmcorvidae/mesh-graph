FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin meshgraph

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install .

# Recommended mount point for the SQLite database and other runtime files.
RUN mkdir -p /data && chown meshgraph:meshgraph /data
VOLUME ["/data"]

# Config via environment variables — all optional except MQTT__BROKER.
# See docker-compose.yml for the full list.
ENV MQTT__BROKER="" \
    MQTT__PORT="1883" \
    MQTT__USERNAME="" \
    MQTT__PASSWORD="" \
    MQTT__TOPIC="msh/#" \
    MQTT__ENCRYPTION_KEY="1PG7OiApB1nwvP+rz05pAQ==" \
    API__HOST="0.0.0.0" \
    API__PORT="8080" \
    DB__PATH="/data/trace-graph.db" \
    OBSERVABILITY__ENABLED="false" \
    OBSERVABILITY__SERVICE_NAME="mesh-graph" \
    OBSERVABILITY__ENVIRONMENT="production" \
    OBSERVABILITY__EXPORTER="otlp" \
    OBSERVABILITY__OTLP_ENDPOINT="http://localhost:4317" \
    OBSERVABILITY__SAMPLE_RATIO="1.0" \
    MESH_GRAPH_MODE="both"

USER meshgraph

ENTRYPOINT ["sh", "-c", "exec mesh-graph --mode $MESH_GRAPH_MODE"]
