from __future__ import annotations
import argparse
import logging
import os
from flask import Flask
from modules.web import create_app

LOGGER = logging.getLogger("emg_rehab.web")


def configure_logging() -> None:
    """Configure concise Flask development-server logs once."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("werkzeug").setLevel(level)


app: Flask = create_app()


def main() -> None:
    """Validate local-service settings and start the local Flask server."""
    parser = argparse.ArgumentParser(description="EMG Rehabilitation web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    arguments = parser.parse_args()

    if not os.environ.get("WEB_PASSWORD"):
        parser.error("請先在 .env 設定 WEB_PASSWORD。")
    if arguments.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("網站只允許在本機監聽；對外服務請透過 HTTPS proxy。")
    if os.environ.get("WEB_BEHIND_HTTPS_PROXY") == "1" and not os.environ.get("WEB_SECRET_KEY"):
        parser.error("HTTPS proxy 模式必須在 .env 設定 WEB_SECRET_KEY。")

    configure_logging()
    LOGGER.info("Starting EMG rehabilitation web service on %s:%s", arguments.host, arguments.port)
    app.run(host=arguments.host, port=arguments.port, debug=False)


if __name__ == "__main__":
    main()
