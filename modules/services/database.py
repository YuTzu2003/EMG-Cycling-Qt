"""SQL Server connectivity shared by website Blueprints.

This module owns connection configuration and transactions only.  Each
Blueprint keeps the SQL for the data it owns.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine

from modules.config import BaseConfig


_engine = None

def get_engine():
    global _engine
    if _engine is None:
        db_uri = BaseConfig.SQLALCHEMY_DATABASE_URI
        if not db_uri:
            raise ValueError("環境變數 SQLALCHEMY_DATABASE_URI 未設定")
        _engine = create_engine(db_uri, pool_pre_ping=True)
    return _engine


def get_conn():
    return get_engine().raw_connection()


def connect():
    """Compatibility name used by the existing Blueprints."""
    return get_conn()


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
