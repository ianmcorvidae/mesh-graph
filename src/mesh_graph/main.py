from __future__ import annotations

import logging
import signal
import sys

import uvicorn

from mesh_graph.api.app import create_app
from mesh_graph.config import load_config
from mesh_graph.db import get_connection, init_db
from mesh_graph.ingestion.mqtt import MQTTDataSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main(config_path: str = "config.toml") -> None:
    cfg = load_config(config_path)

    conn = get_connection(cfg.db.path)
    init_db(conn)

    source = MQTTDataSource(
        broker=cfg.mqtt.broker,
        port=cfg.mqtt.port,
        username=cfg.mqtt.username,
        password=cfg.mqtt.password,
        topic=cfg.mqtt.topic,
        encryption_key=cfg.mqtt.encryption_key,
    )

    logger.info("Starting MQTT ingestion from %s:%d", cfg.mqtt.broker, cfg.mqtt.port)
    source.start(cfg.db.path)

    def _shutdown(sig, frame):
        logger.info("Shutting down…")
        source.stop()
        conn.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    app = create_app(conn)
    logger.info("Starting API on %s:%d", cfg.api.host, cfg.api.port)
    uvicorn.run(app, host=cfg.api.host, port=cfg.api.port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="mesh-graph server")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    args = parser.parse_args()
    main(args.config)
