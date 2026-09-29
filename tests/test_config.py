import textwrap

import pytest

from mesh_graph.config import ConfigError, load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _toml(text: str, tmp_path) -> str:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(text))
    return str(p)


def _clear_env(monkeypatch):
    """Remove all mesh-graph env vars so tests start from a clean slate."""
    for key in [
        "MQTT__BROKER", "MQTT__PORT", "MQTT__USERNAME", "MQTT__PASSWORD",
        "MQTT__TOPIC", "MQTT__ENCRYPTION_KEY",
        "API__HOST", "API__PORT",
        "DB__PATH",
        "OBSERVABILITY__ENABLED", "OBSERVABILITY__SERVICE_NAME",
        "OBSERVABILITY__ENVIRONMENT", "OBSERVABILITY__EXPORTER",
        "OBSERVABILITY__OTLP_ENDPOINT", "OBSERVABILITY__SAMPLE_RATIO",
    ]:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# TOML-only
# ---------------------------------------------------------------------------

def test_valid_full_config_from_toml(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _toml(
        """
        [mqtt]
        broker = "mqtt.example.com"
        port = 1884
        username = "user"
        password = "pass"
        topic = "msh/+/2/e/"
        encryption_key = "1PG7OiApB1nwvP+rz05pAQ=="

        [api]
        host = "127.0.0.1"
        port = 9090

        [db]
        path = "/tmp/test.db"

        [observability]
        enabled = true
        service_name = "mesh-graph-test"
        environment = "ci"
        exporter = "console"
        otlp_endpoint = "http://collector:4317"
        sample_ratio = 0.25
    """,
        tmp_path,
    )
    cfg = load_config(path)
    assert cfg.mqtt.broker == "mqtt.example.com"
    assert cfg.mqtt.port == 1884
    assert cfg.mqtt.username == "user"
    assert cfg.mqtt.password == "pass"
    assert cfg.mqtt.topic == "msh/+/2/e/"
    assert cfg.mqtt.encryption_key == "1PG7OiApB1nwvP+rz05pAQ=="
    assert cfg.api.host == "127.0.0.1"
    assert cfg.api.port == 9090
    assert cfg.db.path == "/tmp/test.db"
    assert cfg.observability.enabled is True
    assert cfg.observability.service_name == "mesh-graph-test"
    assert cfg.observability.environment == "ci"
    assert cfg.observability.exporter == "console"
    assert cfg.observability.otlp_endpoint == "http://collector:4317"
    assert cfg.observability.sample_ratio == 0.25


def test_defaults_applied_from_toml(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _toml(
        """
        [mqtt]
        broker = "mqtt.example.com"
    """,
        tmp_path,
    )
    cfg = load_config(path)
    assert cfg.mqtt.port == 1883
    assert cfg.mqtt.username == ""
    assert cfg.mqtt.password == ""
    assert cfg.mqtt.topic == "msh/#"
    assert cfg.api.host == "0.0.0.0"
    assert cfg.api.port == 8080
    assert cfg.db.path == "trace-graph.db"
    assert cfg.observability.enabled is False
    assert cfg.observability.service_name == "mesh-graph"
    assert cfg.observability.environment == "dev"
    assert cfg.observability.exporter == "otlp"
    assert cfg.observability.otlp_endpoint == "http://127.0.0.1:4317"
    assert cfg.observability.sample_ratio == 1.0


def test_missing_broker_in_toml_raises(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _toml(
        """
        [mqtt]
        port = 1883
    """,
        tmp_path,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_mqtt_section_in_toml_raises(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _toml(
        """
        [api]
        port = 8080
    """,
        tmp_path,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_explicit_file_not_found_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/config.toml")


def test_default_file_not_found_is_silent(monkeypatch, tmp_path):
    """If config.toml doesn't exist at the default path, fall back to env vars."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MQTT__BROKER", "mqtt.example.com")
    # Ensure we're in a directory with no config.toml
    monkeypatch.chdir(tmp_path)
    cfg = load_config("config.toml")
    assert cfg.mqtt.broker == "mqtt.example.com"


# ---------------------------------------------------------------------------
# Env-var-only  (no TOML file)
# ---------------------------------------------------------------------------

def test_valid_full_config_from_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MQTT__BROKER", "mqtt.example.com")
    monkeypatch.setenv("MQTT__PORT", "1884")
    monkeypatch.setenv("MQTT__USERNAME", "user")
    monkeypatch.setenv("MQTT__PASSWORD", "pass")
    monkeypatch.setenv("MQTT__TOPIC", "msh/+/2/e/")
    monkeypatch.setenv("MQTT__ENCRYPTION_KEY", "1PG7OiApB1nwvP+rz05pAQ==")
    monkeypatch.setenv("API__HOST", "127.0.0.1")
    monkeypatch.setenv("API__PORT", "9090")
    monkeypatch.setenv("DB__PATH", "/tmp/test.db")
    monkeypatch.setenv("OBSERVABILITY__ENABLED", "true")
    monkeypatch.setenv("OBSERVABILITY__SERVICE_NAME", "mesh-graph-test")
    monkeypatch.setenv("OBSERVABILITY__ENVIRONMENT", "ci")
    monkeypatch.setenv("OBSERVABILITY__EXPORTER", "console")
    monkeypatch.setenv("OBSERVABILITY__OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OBSERVABILITY__SAMPLE_RATIO", "0.25")

    cfg = load_config(None)  # skip file layer entirely
    assert cfg.mqtt.broker == "mqtt.example.com"
    assert cfg.mqtt.port == 1884
    assert cfg.mqtt.username == "user"
    assert cfg.mqtt.password == "pass"
    assert cfg.api.port == 9090
    assert cfg.db.path == "/tmp/test.db"
    assert cfg.observability.enabled is True
    assert cfg.observability.sample_ratio == 0.25


def test_missing_broker_no_file_no_env_raises(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="MQTT__BROKER"):
        load_config(None)


# ---------------------------------------------------------------------------
# Layered: env vars override TOML values
# ---------------------------------------------------------------------------

def test_env_overrides_toml(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _toml(
        """
        [mqtt]
        broker = "mqtt.from-file.com"
        port = 1883
        username = "file-user"

        [api]
        port = 8080

        [db]
        path = "file.db"
    """,
        tmp_path,
    )
    monkeypatch.setenv("MQTT__BROKER", "mqtt.from-env.com")
    monkeypatch.setenv("API__PORT", "9999")

    cfg = load_config(path)
    # env wins
    assert cfg.mqtt.broker == "mqtt.from-env.com"
    assert cfg.api.port == 9999
    # file value used where no env var set
    assert cfg.mqtt.username == "file-user"
    assert cfg.db.path == "file.db"


def test_env_provides_broker_when_toml_omits_it(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    path = _toml(
        """
        [mqtt]
        port = 1884
    """,
        tmp_path,
    )
    monkeypatch.setenv("MQTT__BROKER", "mqtt.from-env.com")
    cfg = load_config(path)
    assert cfg.mqtt.broker == "mqtt.from-env.com"
    assert cfg.mqtt.port == 1884  # from file


def test_empty_env_overrides_nonempty_toml(tmp_path, monkeypatch):
    """Setting an env var to '' should override a non-empty TOML value."""
    _clear_env(monkeypatch)
    path = _toml(
        """
        [mqtt]
        broker = "mqtt.example.com"
        username = "file-user"
        password = "file-pass"
    """,
        tmp_path,
    )
    monkeypatch.setenv("MQTT__USERNAME", "")
    monkeypatch.setenv("MQTT__PASSWORD", "")
    cfg = load_config(path)
    assert cfg.mqtt.username == ""
    assert cfg.mqtt.password == ""


# ---------------------------------------------------------------------------
# Observability enabled flag parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enabled_val,expected", [
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("false", False),
    ("0", False),
    ("no", False),
])
def test_observability_enabled_env_parsing(monkeypatch, enabled_val, expected):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MQTT__BROKER", "mqtt.example.com")
    monkeypatch.setenv("OBSERVABILITY__ENABLED", enabled_val)
    cfg = load_config(None)
    assert cfg.observability.enabled is expected
