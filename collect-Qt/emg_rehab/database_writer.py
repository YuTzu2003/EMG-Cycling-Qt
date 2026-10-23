"""Qt worker for serial SQL Server measurement writes."""
from __future__ import annotations

from queue import Queue
from typing import Any

from PySide6.QtCore import QThread, Signal

from modules.storage_db import DatabaseConfig, SqlServerStorage


class DatabaseWriter(QThread):
    """Serial database writer. Failures do not stop collection or CSV output."""

    connected = Signal(str)
    failed = Signal(str)
    completed = Signal(str)

    def __init__(self, config: DatabaseConfig) -> None:
        super().__init__()
        self.config = config
        self.queue: Queue[tuple[str, Any]] = Queue()

    def enqueue(self, command: str, payload: Any = None) -> None:
        self.queue.put((command, payload))

    def run(self) -> None:
        store = SqlServerStorage(self.config)
        try:
            store.connect()
            self.connected.emit(store.test_connection())
            completed_message = "資料庫已完成儲存"
            while True:
                command, payload = self.queue.get()
                if command == "start":
                    store.start_session(payload)
                elif command == "rows":
                    store.write_rows(payload[0], payload[1])
                elif command == "finish":
                    store.finish_session(payload)
                elif command == "discard":
                    store.discard_session(str(payload))
                    completed_message = "資料庫：已刪除本次測試資料"
                elif command == "stop":
                    break
            self.completed.emit(completed_message)
        except Exception as exc:
            try:
                if store.connection is not None:
                    store.connection.rollback()
            except Exception:
                pass
            self.failed.emit(str(exc))
        finally:
            store.close()
