# Copilot Instructions for mesh-graph

## Build, Test & Run

### Setup & Installation
- Install dependencies: `uv sync`
- Install with dev tools (pytest, httpx, mocks): `uv sync --extra dev`
- Requires Python 3.11+ and Graphviz (`apt install graphviz` or `brew install graphviz`)

### Running the Server

**Default (both ingestion and API):**
```sh
uv run mesh-graph --config config.toml
```

**Run separately:**
```sh
# Terminal 1: Ingestion only (MQTT data collection to DB)
uv run mesh-graph --config config.toml --mode ingestion

# Terminal 2: API only (serves graph queries from existing DB)
uv run mesh-graph --config config.toml --mode api
```

**Modes:**
- `--mode both` (default): Run MQTT ingestion and HTTP API in the same process
- `--mode ingestion`: Run only the MQTT data collection thread (requires shared DB)
- `--mode api`: Run only the HTTP API server (requires an existing, populated DB)

### Testing
- Full test suite: `uv run pytest`
- Single test file: `uv run pytest tests/test_api.py`
- Single test: `uv run pytest tests/test_api.py::test_function_name`
- Tests use in-memory SQLite DB with fixtures (see `tests/conftest.py`)

## Architecture

### Three-Layer Design

1. **Data Ingestion Layer** (`src/mesh_graph/ingestion/`)
   - Abstract `DataSource` base class with start/stop lifecycle
   - `MQTTDataSource` subscribes to Meshtastic MQTT broker, decrypts packets (AES), and upserts nodes/links to DB
   - Runs in background thread, writes to shared SQLite connection

2. **Persistence Layer** (`src/mesh_graph/db.py`)
   - SQLite with WAL mode and foreign key constraints enabled
   - Three core tables: `traceroute`, `traceroute_link`, `nodes`
   - Provides query helpers: `get_links_for_network`, `get_links_for_trace`, `get_links_for_node`, `get_node_attrs`
   - Composite primary keys enforce data integrity (e.g., trace route uniqueness by `from_id, trace_id, to_id`)

3. **API + Graph Layer** (`src/mesh_graph/api/`, `src/mesh_graph/graph/`)
   - FastAPI server exposing graph visualization and data endpoints
   - Graph builders construct NetworkX graphs from DB links with optional time filtering and directionality
   - Renderer uses Graphviz (pydot) to output PNG or SVG

### Key Data Model
- **Traceroutes**: Each `traceroute` record represents observed hops in a message's route through the network
- **Links**: Individual hops stored in `traceroute_link` with SNR, direction (reply/fast-path), and timestamps
- **Nodes**: Device metadata (name, role) cached from network info packets

### Node ID Format
- Meshtastic uses `!xxxxxxxx` 32-bit hex format (e.g., `!aabbccdd`)
- Parser accepts: `!aabbccdd`, `0x`-prefixed hex, plain hex, or decimal
- Internally stored as integers (64-bit unsigned)

## Key Conventions

### Graph Rendering & Visualization
- **Node coloring**: By node ID (fixed per node)
- **Edge coloring**: Uses XOR of link start/end for deterministic but distinct colors per direction pair
- **SNR labels**: Show signal-to-noise ratio ranges (e.g., `5..8dB`) for aggregated edges
- **Directionality**: Graph builders support `inbound`, `outbound`, `both` (split), or `network` (legacy mixed) modes
- Collapsed network graphs show one edge per direction (aggregating all observed paths)

### Time Parameters
- ISO 8601 format: `2024-01-01T12:00:00Z` (UTC) or with offset (`+02:00`)
- Query string `+` decoding: ISO parser replaces `" "` with `"+"` to handle query encoding
- Cursor pagination: `after=<unix_timestamp>&limit=<1..500>`

### Configuration (TOML)
- Split into `[mqtt]`, `[api]`, `[db]` sections
- MQTT: broker, port, username, password, topic, encryption_key (base64 AES)
- API: host, port
- DB: SQLite path (file or `:memory:`)
- Encryption key defaults to public Meshtastic channel key

### Testing Patterns
- Fixtures in `conftest.py`: in-memory DB with schema pre-initialized
- Tests create FastAPI `TestClient` with injected DB connection
- Mock data uses node IDs like `0xAAAA0001` for readability
- Database fixtures passed to app via `create_app(conn)`
