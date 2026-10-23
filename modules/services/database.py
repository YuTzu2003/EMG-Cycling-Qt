"""SQL Server connectivity shared by website Blueprints."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def connect():
    load_dotenv(ROOT / ".env", override=False)
    password = os.getenv("DB_PASSWORD", "")
    if not password:
        raise ValueError("請在 .env 設定 DB_PASSWORD。")
    import pymssql
    server = os.getenv("DB_SERVER", "127.0.0.1,1433")
    host, separator, port_text = server.rpartition(",")
    host, port = (host, int(port_text)) if separator and port_text.isdecimal() else (server, 1433)
    return pymssql.connect(server=host, port=port, user=os.getenv("DB_USER", "Rehab-User"),
                           password=password, database=os.getenv("DB_NAME", "RehabPatients-ylh"),
                           login_timeout=5, timeout=30)


@contextmanager
def transaction() -> Iterator[object]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
