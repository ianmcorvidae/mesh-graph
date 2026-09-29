from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(Exception):
    pass


@dataclass
class MQTTConfig:
    broker: str
    port: int = 1883
    username: str = ""
    password: str = ""
    topic: str = "msh/#"
    encryption_key: str = "1PG7OiApB1nwvP+rz05pAQ=="


@dataclass
class APIConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class DBConfig:
    path: str = "trace-graph.db"


@dataclass
class ObservabilityConfig:
    enabled: bool = False
    service_name: str = "mesh-graph"
    environment: str = "dev"
    exporter: str = "otlp"
    otlp_endpoint: str = "http://127.0.0.1:4317"
    sample_ratio: float = 1.0


@dataclass
class Config:
    mqtt: MQTTConfig
    api: APIConfig = field(default_factory=APIConfig)
    db: DBConfig = field(default_factory=DBConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)


def _load_toml(path: str, require: bool) -> dict:
    """Load a TOML file and return its contents as a dict.

    If *require* is False and the file doesn't exist, returns an empty dict
    instead of raising (used for the default config.toml path).
    """
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        if require:
            raise ConfigError(f"Config file not found: {path}")
        return {}
    except Exception as e:
        raise ConfigError(f"Failed to parse config file '{path}': {e}") from e


def _env(key: str) -> str | None:
    """Return the env var value, or ``None`` if the variable is not set."""
    return os.environ.get(key)


def _first(*values):
    """Return the first value that is not ``None``."""
    for v in values:
        if v is not None:
            return v
    return None


def load_config(path: str | None = "config.toml") -> Config:
    """Load configuration with a layered resolution strategy.

    Resolution order (highest priority wins):
      1. Environment variables  (MQTT__BROKER, API__PORT, …)
      2. config.toml values     (if *path* points to an existing file)
      3. Built-in defaults

    *path* behaviour:
      - If explicitly provided (not the default) and the file is missing → error.
      - If left as the default ``"config.toml"`` and the file is missing → silently
        skip the file layer and rely on env vars / defaults.
      - Pass ``None`` to skip the file layer entirely (env vars + defaults only).

    The only required value is mqtt.broker / MQTT__BROKER — it must come from
    one of the two sources.
    """
    DEFAULT_PATH = "config.toml"
    explicit_path = path is not None and path != DEFAULT_PATH

    toml: dict = {}
    if path is not None:
        toml = _load_toml(path, require=explicit_path)

    mqtt_toml = toml.get("mqtt", {})
    api_toml = toml.get("api", {})
    db_toml = toml.get("db", {})
    obs_toml = toml.get("observability", {})

    # ------------------------------------------------------------------ mqtt
    broker = _first(_env("MQTT__BROKER"), mqtt_toml.get("broker"))
    if not broker:
        raise ConfigError(
            "Missing required config: set 'mqtt.broker' in config.toml or MQTT__BROKER env var"
        )
    mqtt = MQTTConfig(
        broker=broker,
        port=int(_first(_env("MQTT__PORT"), mqtt_toml.get("port"), 1883)),
        username=_first(_env("MQTT__USERNAME"), mqtt_toml.get("username"), ""),
        password=_first(_env("MQTT__PASSWORD"), mqtt_toml.get("password"), ""),
        topic=_first(_env("MQTT__TOPIC"), mqtt_toml.get("topic"), "msh/#"),
        encryption_key=_first(_env("MQTT__ENCRYPTION_KEY"), mqtt_toml.get("encryption_key"), "1PG7OiApB1nwvP+rz05pAQ=="),
    )

    # ------------------------------------------------------------------- api
    api = APIConfig(
        host=_first(_env("API__HOST"), api_toml.get("host"), "0.0.0.0"),
        port=int(_first(_env("API__PORT"), api_toml.get("port"), 8080)),
    )

    # -------------------------------------------------------------------- db
    db = DBConfig(
        path=_first(_env("DB__PATH"), db_toml.get("path"), "trace-graph.db"),
    )

    # --------------------------------------------------------- observability
    _obs_enabled_env = _env("OBSERVABILITY__ENABLED")
    if _obs_enabled_env is not None:
        obs_enabled = _obs_enabled_env.lower() in ("1", "true", "yes")
    else:
        obs_enabled = obs_toml.get("enabled", False)

    observability = ObservabilityConfig(
        enabled=obs_enabled,
        service_name=_first(_env("OBSERVABILITY__SERVICE_NAME"), obs_toml.get("service_name"), "mesh-graph"),
        environment=_first(_env("OBSERVABILITY__ENVIRONMENT"), obs_toml.get("environment"), "dev"),
        exporter=_first(_env("OBSERVABILITY__EXPORTER"), obs_toml.get("exporter"), "otlp"),
        otlp_endpoint=_first(_env("OBSERVABILITY__OTLP_ENDPOINT"), obs_toml.get("otlp_endpoint"), "http://127.0.0.1:4317"),
        sample_ratio=float(_first(_env("OBSERVABILITY__SAMPLE_RATIO"), obs_toml.get("sample_ratio"), 1.0)),
    )

    return Config(mqtt=mqtt, api=api, db=db, observability=observability)
