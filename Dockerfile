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

USER meshgraph

ENTRYPOINT ["mesh-graph"]
