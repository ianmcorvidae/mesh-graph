# mesh-graph

Listens for Meshtastic traceroute packets over MQTT and exposes network
topology graphs via an HTTP API and web UI.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Graphviz (`apt install graphviz` / `brew install graphviz`)

## Installation

```sh
uv sync                   # install
uv sync --extra dev       # with dev dependencies (pytest, httpx, ruff, pre-commit)
uv run pre-commit install # install commit hooks
```

## Configuration

Copy `config.toml.example` to `config.toml` and set your MQTT broker, topic, and
encryption key. The full set of options with defaults is documented in the
example file.

```toml
[mqtt]
broker = "mqtt.example.com"
topic = "msh/#"
encryption_key = "1PG7OiApB1nwvP+rz05pAQ=="  # base64 AES key; default is the public Meshtastic key
```

## Observability (OpenTelemetry)

Start Jaeger, enable observability in config, then open `http://localhost:16686`:

```sh
docker compose -f docker-compose.observability.yml up -d
```

```toml
[observability]
enabled = true
exporter = "otlp"       # or "console"
otlp_endpoint = "http://127.0.0.1:4317"
```

The `/graph/*` endpoints emit stage spans (parsing, DB, graph build, render).

## Running

```sh
uv run mesh-graph --config config.toml                              # both (default)
uv run mesh-graph --config config.toml --mode ingestion             # MQTT only
uv run mesh-graph --config config.toml --mode api                   # API only (needs existing DB)
```

## Container image

Published to `ghcr.io/ianmcorvidae/mesh-graph:latest` on `main` pushes.
The entrypoint is `mesh-graph`, so pass `--config` and `--mode` directly:

```sh
docker run --rm \
  -v meshgraph-data:/data \
  ghcr.io/ianmcorvidae/mesh-graph:latest \
  --config /config.toml \
  --mode api
```

Mount `/data` for SQLite persistence and point `db.path` at `/data/trace-graph.db`.

## Web UI

A browser-based UI is available at the root URL. It uses HTMX + Alpine.js for
interactivity with no build step.

| Page | Route | Description |
|------|-------|-------------|
| Dashboard | `GET /` | Stats, recent nodes, recent traceroutes, and live-updating 24h network graph |
| Network | `GET /network` | Interactive network graph with time range, SNR labels, client/unknown toggles |
| Nodes | `GET /nodes` | Searchable list of known nodes with HTMX-driven partial updates |
| Node Detail | `GET /nodes/{node_id}` | Node info, neighborhood graph (with depth/direction/time controls), connection table, and recent traceroutes |
| Traceroutes | `GET /traceroutes` | Filterable list of traceroutes (by `from`/`to`) with HTMX partials |
| Traceroute Detail | `GET /traceroutes/{trace_id}` | Trace graph (direction/communities toggles) and sortable link table with fast-path filter |

All graph pages render SVG inline via an `<object>` tag.

## JSON API

### Data

| Endpoint | Description |
|----------|-------------|
| `GET /api/nodes` | JSON list of known nodes, ordered by `last_seen_ts` descending |
| `GET /api/traceroutes` | JSON list of recent traceroutes, ordered by `first_seen_ts` descending |

Both data endpoints support timestamp cursor pagination:

```
?after=1711920000&limit=100
```

- `after`: UNIX timestamp cursor (defaults to current time)
- `limit`: max number of rows to return (`1..500`)

`/api/traceroutes` also supports optional endpoint filters:

```
?from=!aabbccdd&to=!eeff0011
```

- `from`: expected source node (`!xxxxxxxx`, plain hex, `0x` hex, or decimal)
- `to`: expected destination node (same formats)

### Graph endpoints

All graph endpoints accept `?format=svg` (default), `?format=png`, or `?format=dot`.

| Endpoint | Description |
|----------|-------------|
| `GET /graph/network` | Collapsed directional graph focused on backbone nodes (ROUTER, ROUTER_LATE, CLIENT_BASE) |
| `GET /graph/trace/{trace_id}` | Graph for a single traceroute, with uplink nodes labeled with relative receive times |
| `GET /graph/node/{node_id}` | Collapsed neighborhood graph around a specific node |

All graph endpoints accept `?clickable=true` to generate SVG with clickable node links.

`/graph/network` and `/graph/node/{node_id}` accept optional time-range filters:

```
?start=2024-01-01T00:00:00Z&end=2024-01-02T00:00:00Z
```

`/graph/network` also accepts:

```
?snr_labels=true
?include_unknown_nodes=true
?include_clients=true
```

- `snr_labels`: include edge SNR labels (defaults to `false` for readability/performance)
- `include_unknown_nodes`: include synthetic unknown-hop nodes (defaults to `false`)
- `include_clients`: include client/other non-backbone nodes directly (defaults to `false`)

When `include_clients=false`, `/graph/network` keeps only ROUTER/ROUTER_LATE/CLIENT_BASE nodes.

`/graph/trace/{trace_id}` also accepts optional selectors when `trace_id` is not unique:

```
?from=!aabbccdd&to=!eeff0011&date=2024-01-01T00:00:00Z&direction=both&communities=0.5
```

- `from`: expected origin node (`!xxxxxxxx`, plain hex, `0x` hex, or decimal)
- `to`: expected destination node (same formats)
- `date`: approximate traceroute timestamp (ISO 8601); the closest match is selected
- `direction`: `both` (default), `out` (non-reply links only), or `in` (reply links only)
- `communities`: `false` (default, disabled), `true` (resolution 1.0), or a number to tune community detection

When available, trace graph nodes that match an uplink get a label like `Uplink: +4s` (relative to the first observed uplink at `+0s`).

`/graph/node/{node_id}` supports traversal controls:

```
?direction=both&depth=2
```

- `direction`: `inbound`, `outbound`, `both` (default), or `network`
- `depth`: number of hops from the target node (`1..10`, default `1`)

Node graph edges are collapsed per direction (one edge per node pair) with XOR-based link colors and SNR range labels. `direction=both` splits overlapping nodes into `[in]`/`[out]` entries with direction-consistent links; `direction=network` keeps the legacy combined behavior.

Node IDs use the Meshtastic `!xxxxxxxx` hex format, e.g. `/graph/node/!aabbccdd`.

### Examples

```sh
# Network graph with SNR labels
curl "http://localhost:8080/graph/network?format=svg&snr_labels=true" -o network-labeled.svg

# Include client nodes in network graph
curl "http://localhost:8080/graph/network?format=svg&include_clients=true" -o network-with-clients.svg

# Node neighborhood, outbound, 2 hops deep
curl "http://localhost:8080/graph/node/!aabbccdd?format=svg&direction=outbound&depth=2" -o node-outbound.svg

# Trace graph disambiguated by endpoint pair and approximate date
curl "http://localhost:8080/graph/trace/12345?from=!aabbccdd&to=!eeff0011&date=2024-01-01T12:00:00Z" -o trace.svg

# Network graph for the last hour as PNG
curl "http://localhost:8080/graph/network?format=png&start=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" -o recent.png
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
  main.py            # Entry point
  observability.py   # OpenTelemetry setup
  utils.py           # ID formatting, time parsing
  api/
    app.py           # FastAPI application
    models.py        # Pydantic response models
    ui.py            # Web UI routes (Jinja2/HTMX/Alpine.js)
    static/
      style.css      # UI stylesheet
    templates/
      base.html                 # Layout with nav, HTMX & Alpine.js
      dashboard.html            # Dashboard page
      network.html              # Network graph page
      nodes_list.html           # Nodes list page
      node_detail.html          # Node detail page
      traceroutes_list.html     # Traceroutes list page
      traceroute_detail.html    # Traceroute detail page
      partials/
        node_table_rows.html    # HTMX partial for node rows
        trace_table_rows.html   # HTMX partial for trace rows
  graph/
    builder.py       # NetworkX graph construction
    renderer.py      # PNG/SVG rendering
  ingestion/
    base.py          # DataSource abstract base class
    mqtt.py          # MQTT ingestion implementation
tests/               # pytest test suite
```
