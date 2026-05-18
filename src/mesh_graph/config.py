from __future__ import annotations

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
class Config:
    mqtt: MQTTConfig
    api: APIConfig = field(default_factory=APIConfig)
    db: DBConfig = field(default_factory=DBConfig)


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except Exception as e:
        raise ConfigError(f"Failed to parse config: {e}") from e

    mqtt_data = data.get("mqtt")
    if not mqtt_data:
        raise ConfigError("Missing required [mqtt] section in config")
    if "broker" not in mqtt_data:
        raise ConfigError("Missing required field: mqtt.broker")

    mqtt = MQTTConfig(**{k: v for k, v in mqtt_data.items() if k in MQTTConfig.__dataclass_fields__})
    api = APIConfig(**{k: v for k, v in data.get("api", {}).items() if k in APIConfig.__dataclass_fields__})
    db = DBConfig(**{k: v for k, v in data.get("db", {}).items() if k in DBConfig.__dataclass_fields__})

    return Config(mqtt=mqtt, api=api, db=db)
