import textwrap
import pytest
from mesh_graph.config import load_config, ConfigError


def _toml(text: str, tmp_path) -> str:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(text))
    return str(p)


def test_valid_full_config(tmp_path):
    path = _toml("""
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
    """, tmp_path)
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


def test_defaults_applied(tmp_path):
    path = _toml("""
        [mqtt]
        broker = "mqtt.example.com"
    """, tmp_path)
    cfg = load_config(path)
    assert cfg.mqtt.port == 1883
    assert cfg.mqtt.username == ""
    assert cfg.mqtt.password == ""
    assert cfg.mqtt.topic == "msh/#"
    assert cfg.api.host == "0.0.0.0"
    assert cfg.api.port == 8080
    assert cfg.db.path == "trace-graph.db"


def test_missing_broker_raises(tmp_path):
    path = _toml("""
        [mqtt]
        port = 1883
    """, tmp_path)
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_mqtt_section_raises(tmp_path):
    path = _toml("""
        [api]
        port = 8080
    """, tmp_path)
    with pytest.raises(ConfigError):
        load_config(path)


def test_file_not_found_raises():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.toml")
