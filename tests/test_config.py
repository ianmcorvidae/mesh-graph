import textwrap

import pytest

from mesh_graph.config import ConfigError, load_config


def _toml(text: str, tmp_path) -> str:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(text))
    return str(p)


def test_valid_full_config(tmp_path):
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


def test_defaults_applied(tmp_path):
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


def test_missing_broker_raises(tmp_path):
    path = _toml(
        """
        [mqtt]
        port = 1883
    """,
        tmp_path,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_mqtt_section_raises(tmp_path):
    path = _toml(
        """
        [api]
        port = 8080
    """,
        tmp_path,
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_file_not_found_raises():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.toml")
