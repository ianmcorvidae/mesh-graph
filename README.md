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

Or directly:

```sh
uv run python -m mesh_graph.main --config config.toml
```

## API

All graph endpoints accept `?format=png` (default) or `?format=svg`.

### Graphs

| Endpoint | Description |
|----------|-------------|
| `GET /graph/network` | Full network-wide graph of all observed routes |
| `GET /graph/trace/{trace_id}` | Graph for a single traceroute |
| `GET /graph/node/{node_id}` | All routes through a specific node |

`/graph/network` and `/graph/node/{node_id}` accept optional time-range filters:

```
?start=2024-01-01T00:00:00Z&end=2024-01-02T00:00:00Z
```

Node IDs use the Meshtastic `!xxxxxxxx` hex format, e.g. `/graph/node/!aabbccdd`.

### Data

| Endpoint | Description |
|----------|-------------|
| `GET /nodes` | JSON list of known nodes |
| `GET /traceroutes` | JSON list of recent traceroutes |

### Examples

```sh
# Save current network graph as PNG
curl http://localhost:8080/graph/network -o network.png

# SVG of all routes through a specific node
curl "http://localhost:8080/graph/node/!aabbccdd?format=svg" -o node.svg

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
