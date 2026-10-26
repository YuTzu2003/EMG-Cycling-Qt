from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import Queue
from threading import Lock
from typing import Any
from uuid import uuid4
import math
from PySide6.QtCore import QThread, Signal

def prefixed_id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"

def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def quality_code(value: object, default: str = "UNKNOWN") -> str:
    """Translate display text to stable English database quality codes."""
    text = str(value or "").strip()
    codes = {
        "有效": "VALID", "無基準值": "NO_BASELINE", "基準值過低": "BASELINE_TOO_LOW",
        "基準不相容": "BASELINE_INCOMPATIBLE", "EMG 資料不足": "INSUFFICIENT_DATA",
        "估計（總功率 × 平衡）": "VALID", "僅總功率／踏頻": "VALID",
    }
    return codes.get(text, text if text.isupper() else default)

@dataclass(frozen=True)
class DatabaseConfig:
    server: str
    database: str = "master"
    username: str = ""
    password: str = ""

    def is_complete(self) -> bool:
        return bool(self.server.strip() and self.username.strip() and self.password)
    def connection_string(self) -> str:
        def escape(value: str) -> str:
            return "{" + value.replace("}", "}}") + "}"
        parts = [f"Server={escape(self.server)}", f"Database={escape(self.database)}",
                 f"UID={escape(self.username)}", f"PWD={escape(self.password)}",
                 "Encrypt=yes", "TrustServerCertificate=yes"]
        return ";".join(parts) + ";"


class SqlServerStorage:
    required_tables = {"Patients", "Sessions", "TestBaselines", "EMGData", "PedalData", "TrainerData"}
    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        self.connection: Any | None = None

    @staticmethod
    def _driver() -> Any:
        try:
            import mssql_python
        except ImportError as exc:  # keeps the main app usable until installed
            raise RuntimeError("找不到 mssql-python。請執行 uv sync 安裝 SQL Server 支援。") from exc
        return mssql_python

    def connect(self) -> None:
        if not self.config.is_complete():
            raise ValueError("請填寫 SQL Server、資料庫與登入資訊。")
        self.connection = self._driver().connect(self.config.connection_string(), timeout=5)

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _cursor(self) -> Any:
        if self.connection is None:
            self.connect()
        return self.connection.cursor()

    def test_connection(self) -> str:
        cursor = self._cursor()
        rows = cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo'").fetchall()
        found = {str(row[0]) for row in rows}
        missing = self.required_tables - found
        if missing:
            raise RuntimeError("缺少資料表：" + "、".join(sorted(missing)))
        name = cursor.execute("SELECT DB_NAME()").fetchone()[0]
        return str(name)

    def list_databases(self) -> list[str]:
        """Return only online databases this SQL login can access."""
        cursor = self._cursor()
        rows = cursor.execute("SELECT name FROM sys.databases WHERE state_desc='ONLINE' AND HAS_DBACCESS(name)=1 ORDER BY name").fetchall()
        return [str(row[0]) for row in rows]

    def list_patients(self) -> list[dict[str, str]]:
        """Read the shared patient registry maintained by the website."""
        rows = self._cursor().execute(
            "SELECT PatientID, Name, Sex FROM dbo.Patients ORDER BY Name, PatientID"
        ).fetchall()
        return [{"patient_id": str(row[0]), "name": str(row[1] or "").strip(),
                 "sex": str(row[2] or "未指定").strip()} for row in rows]

    def start_session(self, payload: dict[str, Any], commit: bool = True) -> None:
        cursor = self._cursor()
        patient = cursor.execute(
            "SELECT Name, Sex FROM dbo.Patients WITH (HOLDLOCK) WHERE PatientID=?", payload["patient_id"]
        ).fetchone()
        if patient is None:
            raise ValueError("此受測者不存在，請先在網頁建立，再於 Qt 重新載入受測者。")
        if (str(patient[0] or "").strip() != (payload.get("name") or "").strip()
                or str(patient[1] or "未指定").strip() != (payload.get("sex") or "未指定").strip()):
            raise ValueError("網頁的受測者資料已變更，請確認本次測試的受測者資料後再儲存。")
        cursor.execute("""INSERT INTO dbo.Sessions(TestID, PatientID, Name, Sex, ActionType, TestTime, ResistanceLevel, HasBaseline, Status, Notes, CreatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            payload["test_id"], payload["patient_id"], payload.get("name"), payload.get("sex"),
            payload.get("action_type"), payload.get("test_time"), payload.get("resistance_level"),
            bool(payload.get("has_baseline")), "進行中", payload.get("notes"), payload["started_at"], )
        for item in payload.get("baselines", []):
            cursor.execute(
                """INSERT INTO dbo.TestBaselines
                (BaseID, TestID, MuscleName, BaseTime, ActionType, BaseRMSmV,
                 YellowDeviationPct, RedDeviationPct, QualityStatus, CreatedAt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                prefixed_id("BASE"), payload["test_id"], item["muscle_name"], item.get("base_time"),
                item.get("action_type"), item.get("rms_mv"), item.get("yellow_pct"),
                item.get("red_pct"), quality_code(item.get("quality_status"), "VALID"), payload["started_at"],
            )
        if commit:
            self.connection.commit()

    def _upsert(self, table: str, key_fields: tuple[str, ...], values: dict[str, Any], prefix: str) -> None:
        cursor = self._cursor()
        where = " AND ".join(f"{field}=?" for field in key_fields)
        key_values = [values[field] for field in key_fields]
        update_fields = [field for field in values if field not in key_fields]
        update = ", ".join(f"{field}=?" for field in update_fields)
        cursor.execute(f"UPDATE dbo.{table} SET {update} WHERE {where}",
                       *[values[field] for field in update_fields], *key_values)
        if cursor.rowcount == 0:
            fields = [f"{table[:-4]}DataID" if table != "EMGData" else "EMGDataID", *values.keys()]
            # PedalData and TrainerData use their table name as their key, unlike EMGData.
            fields[0] = {"EMGData": "EMGDataID", "PedalData": "PedalDataID", "TrainerData": "TrainerDataID"}[table]
            cursor.execute(f"INSERT INTO dbo.{table} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                           prefixed_id(prefix), *values.values())

    def write_rows(self, kind: str, rows: list[dict[str, Any]], commit: bool = True) -> None:
        if not rows:
            return
        if kind == "emg":
            for row in rows:
                self._upsert("EMGData", ("TestID", "TimeOffsetSec", "MuscleName"), row, "EMG")
        elif kind == "pedal":
            for row in rows:
                self._upsert("PedalData", ("TestID", "TimeOffsetSec"), row, "PEDAL")
        elif kind == "trainer":
            for row in rows:
                self._upsert("TrainerData", ("TestID", "TimeOffsetSec"), row, "TRAINER")
        else:
            raise ValueError(f"Unsupported database row type: {kind}")
        if commit:
            self.connection.commit()

    def finish_session(self, payload: dict[str, Any], commit: bool = True) -> None:
        cursor = self._cursor()
        cursor.execute(
            """UPDATE dbo.Sessions SET TestTime=?, Status=?, AvgPowerW=?, MaxPowerW=?,
            AvgCadenceRPM=?, TotalDistanceKm=?, AvgLeftBalancePct=?, AvgRightBalancePct=?, Notes=?
            WHERE TestID=?""",
            payload.get("test_time"), payload.get("status"), payload.get("avg_power_w"),
            payload.get("max_power_w"), payload.get("avg_cadence_rpm"), payload.get("distance_km"),
            payload.get("left_balance_pct"), payload.get("right_balance_pct"), payload.get("notes"),
            payload["test_id"],
        )
        if commit:
            self.connection.commit()

    def save_session_bundle(self, payload: tuple[dict, dict[str, list[dict]], dict]) -> None:
        """Commit an approved test as one database transaction."""
        start, rows_by_kind, finish = payload
        try:
            self.start_session(start, commit=False)
            for kind, rows in rows_by_kind.items():
                self.write_rows(kind, rows, commit=False)
            self.finish_session(finish, commit=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def discard_session(self, test_id: str) -> None:
        cursor = self._cursor()
        try:
            for table in ("EMGData", "PedalData", "TrainerData", "TestBaselines", "Sessions"):
                cursor.execute(f"DELETE FROM dbo.{table} WHERE TestID=?", test_id)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def list_sessions(self, patient_id: str, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self._cursor()
        rows = cursor.execute(
            """SELECT TOP (?) TestID, PatientID, Name, CreatedAt, Status, ResistanceLevel,
            AvgPowerW, AvgCadenceRPM, AvgLeftBalancePct, AvgRightBalancePct, TestTime
            FROM dbo.Sessions WHERE PatientID=? ORDER BY CreatedAt DESC""", limit, patient_id).fetchall()
        keys = ("test_id", "patient_id", "name", "created_at", "status", "resistance_level",
                "avg_power_w", "avg_cadence_rpm", "left_balance_pct", "right_balance_pct", "test_time")
        return [dict(zip(keys, row)) for row in rows]

    def list_patients_with_emg(self) -> list[dict[str, Any]]:
        """Return patients that have at least one stored EMG RMS measurement."""
        cursor = self._cursor()
        rows = cursor.execute(
            """SELECT s.PatientID, MAX(s.Name) AS Name, COUNT(DISTINCT s.TestID) AS TestCount
            FROM dbo.Sessions AS s
            WHERE EXISTS (SELECT 1 FROM dbo.EMGData AS e WHERE e.TestID=s.TestID AND e.AvgRMSmV IS NOT NULL)
            GROUP BY s.PatientID ORDER BY s.PatientID"""
        ).fetchall()
        return [dict(zip(("patient_id", "name", "test_count"), row)) for row in rows]

    def list_emg_sessions(self, patient_id: str) -> list[dict[str, Any]]:
        """Return usable stored RMS sessions for one patient, newest first."""
        cursor = self._cursor()
        rows = cursor.execute(
            """SELECT s.TestID, s.CreatedAt, s.TestTime, s.ResistanceLevel, s.Status, COUNT(e.EMGDataID) AS RmsRows
            FROM dbo.Sessions AS s
            INNER JOIN dbo.EMGData AS e ON e.TestID=s.TestID AND e.AvgRMSmV IS NOT NULL
            WHERE s.PatientID=?
            GROUP BY s.TestID, s.CreatedAt, s.TestTime, s.ResistanceLevel, s.Status
            ORDER BY s.CreatedAt DESC""", patient_id
        ).fetchall()
        keys = ("test_id", "created_at", "test_time", "resistance_level", "status", "rms_rows")
        return [dict(zip(keys, row)) for row in rows]

    def load_emg_rms_series(self, test_id: str) -> list[dict[str, Any]]:
        """Load measured one-second RMS only; no baseline normalization or interpolation."""
        cursor = self._cursor()
        rows = cursor.execute(
            """SELECT MuscleName, TimeOffsetSec, AvgRMSmV, QualityStatus
            FROM dbo.EMGData
            WHERE TestID=? AND AvgRMSmV IS NOT NULL
            ORDER BY MuscleName, TimeOffsetSec""", test_id
        ).fetchall()
        keys = ("muscle_name", "time_s", "rms_mv", "quality_status")
        return [dict(zip(keys, row)) for row in rows]

    def compare_same_resistance(self, patient_id: str, resistance_level: float, limit: int = 20) -> list[dict[str, Any]]:
        """Return summary metrics for a patient under the same fixed resistance.

        This small repository method is intentionally UI-neutral so a future
        FastAPI or mobile client can use the exact same historical query.
        """
        cursor = self._cursor()
        rows = cursor.execute(
            """SELECT TOP (?) TestID, CreatedAt, TestTime, AvgPowerW, MaxPowerW,
            AvgCadenceRPM, AvgLeftBalancePct, AvgRightBalancePct, Status
            FROM dbo.Sessions WHERE PatientID=? AND ResistanceLevel=?
            ORDER BY CreatedAt DESC""", limit, patient_id, resistance_level).fetchall()
        keys = ("test_id", "created_at", "test_time", "avg_power_w", "max_power_w",
                "avg_cadence_rpm", "left_balance_pct", "right_balance_pct", "status")
        return [dict(zip(keys, row)) for row in rows]


class SecondAggregator:
    """Groups actual received samples by second; it never manufactures samples."""
    def __init__(self, test_id: str, started_at: datetime) -> None:
        self.test_id, self.started_at = test_id, started_at
        self._samples: dict[str, dict[int, list[dict[str, Any]]]] = {"emg": {}, "pedal": {}, "trainer": {}}
        self._lock = Lock()

    def add(self, kind: str, time_s: float, sample: dict[str, Any]) -> list[dict[str, Any]]:
        second = max(0, int(time_s))
        with self._lock:
            bucket = self._samples[kind]
            bucket.setdefault(second, []).append(sample)
            return self._flush_before(kind, second)

    def flush_all(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {kind: self._flush_before(kind, 10**9) for kind in self._samples}

    def _flush_before(self, kind: str, current_second: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        bucket = self._samples[kind]
        for second in sorted([key for key in bucket if key < current_second]):
            samples = bucket.pop(second)
            if kind == "emg":
                by_muscle: dict[str, list[dict[str, Any]]] = {}
                for sample in samples:
                    by_muscle.setdefault(sample["muscle_name"], []).append(sample)
                for muscle, values in by_muscle.items():
                    rms = [value for item in values if (value := _number(item.get("rms_mv"))) is not None]
                    if not rms:
                        continue
                    last = values[-1]
                    result.append({"TestID": self.test_id, "TimeOffsetSec": second, "MuscleName": muscle,
                        "AvgRMSmV": sum(rms) / len(rms), "MaxRMSmV": max(rms),
                        "BaseRMSmV": _number(last.get("base_rms_mv")),
                        "DeviationPct": _number(last.get("deviation_pct")),
                        "AlertStatus": last.get("alert_status", "NONE"),
                        "QualityStatus": quality_code(last.get("quality_status"), "UNKNOWN"),
                        "RecordedAt": self.started_at + timedelta(seconds=second)})
            else:
                last = samples[-1]
                def average(key: str) -> float | None:
                    values = [value for item in samples if (value := _number(item.get(key))) is not None]
                    return sum(values) / len(values) if values else None
                base = {"TestID": self.test_id, "TimeOffsetSec": second,
                        "RecordedAt": self.started_at + timedelta(seconds=second)}
                if kind == "pedal":
                    base.update({"TotalPowerW": average("power_w"), "LeftPowerW": average("left_power_w"),
                        "RightPowerW": average("right_power_w"), "LeftBalancePct": average("left_balance_percent"),
                        "RightBalancePct": average("right_balance_percent"), "CadenceRPM": average("cadence_rpm"),
                        "QualityStatus": quality_code(last.get("quality_status"), "UNKNOWN")})
                else:
                    base.update({"DistanceKm": _number(last.get("distance_km")), "SpeedKmh": average("speed_kph"),
                        "PowerW": average("power_w"), "CadenceRPM": average("cadence_rpm"),
                        "QualityStatus": quality_code(last.get("quality_status"), "VALID")})
                result.append(base)
        return result


class DatabaseWriter(QThread):
    """Serial database writer.  Failures do not stop collection or CSV output."""
    connected = Signal(str)
    failed = Signal(str)
    completed = Signal(str)
    patients_loaded = Signal(list)

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
                elif command == "save":
                    store.save_session_bundle(payload)
                elif command == "patients":
                    self.patients_loaded.emit(store.list_patients())
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
