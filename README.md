# mesh-graph

Listens for Meshtastic traceroute packets over MQTT and exposes on-demand
network topology graphs via an HTTP API.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Graphviz (`apt install graphviz` / `brew install graphviz`)

## Installation

```sh
uv sync
```

For development (includes pytest, httpx, etc.):

```sh
uv sync --extra dev
```

## Configuration

Copy `config.toml.example` to `config.toml` and fill in your broker details:

```toml
[mqtt]
broker = "mqtt.example.com"
port = 1883
username = ""
password = ""
topic = "msh/#"
encryption_key = "1PG7OiApB1nwvP+rz05pAQ=="

[api]
host = "0.0.0.0"
port = 8080

[db]
path = "trace-graph.db"
```

`encryption_key` is the base64-encoded AES key for the Meshtastic channel.
The default value is the public default Meshtastic key.

## Running

```sh
uv run mesh-graph --config config.toml
```

Or run as separate processes:

```sh
# Terminal 1: MQTT ingestion only
uv run mesh-graph --config config.toml --mode ingestion

# Terminal 2: API only (requires existing DB from ingestion)
uv run mesh-graph --config config.toml --mode api
```

### Run Modes

- `--mode both` (default): MQTT ingestion and HTTP API in a single process
- `--mode ingestion`: MQTT data collection only (writes to DB)
- `--mode api`: HTTP API server only (reads from DB)

## API

All graph endpoints accept `?format=png` (default) or `?format=svg`.

### Graphs

| Endpoint | Description |
|----------|-------------|
| `GET /graph/network` | Full network-wide graph of all observed routes |
| `GET /graph/network/simple` | Collapsed directional graph (one edge per direction) with SNR range labels |
| `GET /graph/trace/{trace_id}` | Graph for a single traceroute (most recent match by default) |
| `GET /graph/node/{node_id}` | Collapsed neighborhood graph around a specific node |

`/graph/network`, `/graph/network/simple`, and `/graph/node/{node_id}` accept optional time-range filters:

```
?start=2024-01-01T00:00:00Z&end=2024-01-02T00:00:00Z
```

`/graph/trace/{trace_id}` also accepts optional selectors when `trace_id` is not unique:

```
?from=!aabbccdd&to=!eeff0011&date=2024-01-01T00:00:00Z
```

- `from`: expected origin node (`!xxxxxxxx`, plain hex, `0x` hex, or decimal)
- `to`: expected destination node (same formats)
- `date`: approximate traceroute timestamp (ISO 8601); the closest match is selected

`/graph/node/{node_id}` supports traversal controls:

```
?direction=both&depth=2
```

- `direction`: `inbound`, `outbound`, `both` (default), or `network`
- `depth`: number of hops from the target node (`1..10`, default `1`)

Node graph edges are collapsed per direction (one edge per node pair), use XOR-based link colors, and label the observed SNR range. This aggregation ignores whether the data came from outbound vs return traceroute paths.

`direction=both` is the union of inbound and outbound *as separate parts*: overlapping nodes are split into `[in]` and `[out]` entries and each part keeps only links consistent with its direction. `direction=network` keeps the legacy mixed behavior (combined graph without splitting).

Node IDs use the Meshtastic `!xxxxxxxx` hex format, e.g. `/graph/node/!aabbccdd`.

### Data

| Endpoint | Description |
|----------|-------------|
| `GET /nodes` | JSON list of known nodes, ordered by `last_seen_ts` descending |
| `GET /traceroutes` | JSON list of recent traceroutes, ordered by `first_seen_ts` descending |

Both data endpoints support timestamp cursor pagination:

```
?after=1711920000&limit=100
```

- `after`: UNIX timestamp cursor (defaults to current time)
- `limit`: max number of rows to return (`1..500`)

`/traceroutes` also supports optional endpoint filters:

```
?from=!aabbccdd&to=!eeff0011
```

- `from`: expected source node (`!xxxxxxxx`, plain hex, `0x` hex, or decimal)
- `to`: expected destination node (same formats)

### Examples

```sh
# Save current network graph as PNG
curl http://localhost:8080/graph/network -o network.png

# Save simplified network graph as SVG (collapsed links + SNR ranges)
curl "http://localhost:8080/graph/network/simple?format=svg" -o network-simple.svg

# SVG of all routes through a specific node
curl "http://localhost:8080/graph/node/!aabbccdd?format=svg" -o node.svg

# Outbound neighborhood up to 2 hops from a node
curl "http://localhost:8080/graph/node/!aabbccdd?format=svg&direction=outbound&depth=2" -o node-outbound.svg

# Trace graph disambiguated by endpoint pair and approximate date
curl "http://localhost:8080/graph/trace/12345?from=!aabbccdd&to=!eeff0011&date=2024-01-01T12:00:00Z" -o trace.svg

# Network graph for the last hour
curl "http://localhost:8080/graph/network?start=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" -o recent.png
```

## Running tests

```sh
uv run pytest
```

## Project layout

```
src/mesh_graph/
  config.py          # Config loading (TOML)
  db.py              # SQLite schema and query helpers
  ingestion/
    base.py          # DataSource abstract base class
    mqtt.py          # MQTT ingestion implementation
  graph/
    builder.py       # NetworkX graph construction
    renderer.py      # PNG/SVG rendering
  api/
    app.py           # FastAPI application
    models.py        # Pydantic response models
  main.py            # Entry point
tests/               # pytest test suite
legacy/              # Original single-file script (reference only)
```
