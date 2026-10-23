"""Shared SQL Server archive for patient records and complete recordings."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from emg_rehab.database.storage_db import DatabaseConfig, SqlServerStorage

# The Qt application reads the workspace configuration without importing any
# runtime code from the web or analysis projects.
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

def default_config() -> DatabaseConfig:
    return DatabaseConfig(
        server=os.getenv("DB_SERVER", "127.0.0.1,1433"),
        database=os.getenv("DB_NAME", "RehabPatients-ylh"),
        username=os.getenv("DB_USER", "Rehab-User"),
        password=os.getenv("DB_PASSWORD", ""),
    )


class Archive:
    def __init__(self, config: DatabaseConfig | None = None):
        self.config = config or default_config()

    @contextmanager
    def connection(self):
        store = SqlServerStorage(self.config)
        try:
            store.connect()
            cursor = store.connection.cursor()
            cursor.execute("""IF OBJECT_ID(N'dbo.Patients', N'U') IS NULL
                CREATE TABLE dbo.Patients (
                  PatientID nvarchar(50) NOT NULL PRIMARY KEY,
                  Name nvarchar(100) NOT NULL,
                  Sex nvarchar(10) NOT NULL DEFAULT N'未指定',
                  Notes nvarchar(1000) NOT NULL DEFAULT N'',
                  CreatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME())""")
            cursor.execute("""IF OBJECT_ID(N'dbo.SessionArchives', N'U') IS NULL
                CREATE TABLE dbo.SessionArchives (
                  TestID nvarchar(50) NOT NULL PRIMARY KEY,
                  PatientID nvarchar(50) NOT NULL REFERENCES dbo.Patients(PatientID),
                  StartedAt nvarchar(64) NOT NULL,
                  Source nvarchar(20) NOT NULL,
                  Status nvarchar(20) NOT NULL,
                  SessionJSON nvarchar(max) NOT NULL,
                  CreatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME())""")
            cursor.execute("""IF OBJECT_ID(N'dbo.RawFiles', N'U') IS NULL
                CREATE TABLE dbo.RawFiles (
                  TestID nvarchar(50) NOT NULL REFERENCES dbo.SessionArchives(TestID) ON DELETE CASCADE,
                  Kind nvarchar(20) NOT NULL,
                  FileName nvarchar(255) NOT NULL,
                  Content varbinary(max) NOT NULL,
                  CONSTRAINT PK_RawFiles PRIMARY KEY (TestID, Kind))""")
            cursor.execute("""INSERT INTO dbo.Patients (PatientID, Name, Sex)
                SELECT s.PatientID, COALESCE(MAX(NULLIF(s.Name, N'')), s.PatientID),
                  COALESCE(MAX(NULLIF(s.Sex, N'')), N'未指定')
                FROM dbo.Sessions s
                WHERE NOT EXISTS (SELECT 1 FROM dbo.Patients p WHERE p.PatientID=s.PatientID)
                GROUP BY s.PatientID""")
            store.connection.commit()
            yield store.connection
            store.connection.commit()
        except Exception:
            if store.connection is not None:
                store.connection.rollback()
            raise
        finally:
            store.close()

    @staticmethod
    def rows(cursor, values):
        keys = [column[0] for column in cursor.description]
        return [dict(zip(keys, row)) for row in values]

    def list_patients(self) -> list[dict]:
        with self.connection() as db:
            cursor = db.cursor()
            values = cursor.execute("""SELECT PatientID AS patient_id, Name AS name,
                Sex AS gender, Notes AS notes FROM dbo.Patients ORDER BY Name, PatientID""").fetchall()
            return self.rows(cursor, values)

    def get_patient(self, patient_id: str) -> dict | None:
        with self.connection() as db:
            cursor = db.cursor()
            row = cursor.execute("""SELECT PatientID AS patient_id, Name AS name,
                Sex AS gender, Notes AS notes FROM dbo.Patients WHERE PatientID=?""", patient_id).fetchone()
            return self.rows(cursor, [row])[0] if row else None

    def save_patient(self, patient_id: str, name: str, gender: str = "未指定", notes: str = "") -> None:
        patient_id, name = patient_id.strip(), name.strip()
        if not patient_id or not name or len(patient_id) > 50 or len(name) > 100:
            raise ValueError("病人 ID 和姓名必填，且不可超過 50／100 字。")
        if gender not in ("未指定", "男性", "女性", "其他"):
            raise ValueError("性別選項無效。")
        with self.connection() as db:
            cursor = db.cursor()
            cursor.execute("UPDATE dbo.Patients SET Name=?, Sex=?, Notes=? WHERE PatientID=?",
                           name, gender, notes.strip()[:1000], patient_id)
            if cursor.rowcount == 0:
                cursor.execute("INSERT INTO dbo.Patients (PatientID, Name, Sex, Notes) VALUES (?, ?, ?, ?)",
                               patient_id, name, gender, notes.strip()[:1000])

    def save_recording(self, session: dict, files: dict[str, Path | tuple[str, bytes]], *,
                       session_id: str | None = None, source: str = "qt", status: str = "完成") -> str:
        patient_id = str((session.get("subject") or {}).get("patientId") or "").strip()
        session_id = session_id or "TEST-" + str(uuid4())
        document = json.dumps(session, ensure_ascii=False, allow_nan=False)
        prepared = {}
        for kind, value in files.items():
            if kind not in ("emg", "pedal", "trainer", "angle", "info"):
                raise ValueError("未知的原始檔種類。")
            prepared[kind] = (value.name, value.read_bytes()) if isinstance(value, Path) else value
        with self.connection() as db:
            cursor = db.cursor()
            if not cursor.execute("SELECT 1 FROM dbo.Patients WHERE PatientID=?", patient_id).fetchone():
                raise ValueError("請先在網站建立病人，再開始收集或匯入資料。")
            if cursor.execute("SELECT 1 FROM dbo.SessionArchives WHERE TestID=?", session_id).fetchone():
                cursor.execute("UPDATE dbo.SessionArchives SET SessionJSON=?, Status=? WHERE TestID=?",
                               document, status, session_id)
            else:
                cursor.execute("""INSERT INTO dbo.SessionArchives
                    (TestID, PatientID, StartedAt, Source, Status, SessionJSON)
                    VALUES (?, ?, ?, ?, ?, ?)""", session_id, patient_id,
                    str(session.get("started_at") or datetime.now(timezone.utc).isoformat()),
                    source, status, document)
            for kind, (filename, content) in prepared.items():
                if len(filename) > 255:
                    raise ValueError("檔名不可超過 255 字。")
                cursor.execute("DELETE FROM dbo.RawFiles WHERE TestID=? AND Kind=?", session_id, kind)
                cursor.execute("INSERT INTO dbo.RawFiles (TestID, Kind, FileName, Content) VALUES (?, ?, ?, ?)",
                               session_id, kind, filename, content)
        return session_id

    def list_sessions(self, patient_id: str | None = None) -> list[dict]:
        with self.connection() as db:
            cursor = db.cursor()
            query = """SELECT a.TestID AS session_id, a.PatientID AS patient_id,
                p.Name AS name, a.StartedAt AS started_at, a.Source AS source,
                a.Status AS status, (SELECT COUNT(*) FROM dbo.RawFiles f
                WHERE f.TestID=a.TestID) AS file_count
                FROM dbo.SessionArchives a JOIN dbo.Patients p ON p.PatientID=a.PatientID"""
            values = (cursor.execute(query + " WHERE a.PatientID=? ORDER BY a.StartedAt DESC", patient_id)
                      if patient_id else cursor.execute(query + " ORDER BY a.StartedAt DESC")).fetchall()
            result = self.rows(cursor, values)
            legacy = """SELECT s.TestID AS session_id, s.PatientID AS patient_id,
                COALESCE(s.Name, p.Name) AS name, CONVERT(nvarchar(64), s.CreatedAt, 126) AS started_at,
                N'legacy' AS source, COALESCE(s.Status, N'完成') AS status,
                0 AS file_count FROM dbo.Sessions s JOIN dbo.Patients p ON p.PatientID=s.PatientID
                WHERE NOT EXISTS (SELECT 1 FROM dbo.SessionArchives a WHERE a.TestID=s.TestID)
                AND (EXISTS (SELECT 1 FROM dbo.EMGData e WHERE e.TestID=s.TestID)
                  OR EXISTS (SELECT 1 FROM dbo.PedalData d WHERE d.TestID=s.TestID)
                  OR EXISTS (SELECT 1 FROM dbo.TrainerData t WHERE t.TestID=s.TestID))"""
            values = (cursor.execute(legacy + " AND s.PatientID=? ORDER BY s.CreatedAt DESC", patient_id)
                      if patient_id else cursor.execute(legacy + " ORDER BY s.CreatedAt DESC")).fetchall()
            result.extend(self.rows(cursor, values))
            return sorted(result, key=lambda item: item["started_at"] or "", reverse=True)

    def get_session(self, session_id: str) -> tuple[dict, dict] | None:
        with self.connection() as db:
            cursor = db.cursor()
            row = cursor.execute("""SELECT TestID AS session_id, PatientID AS patient_id,
                StartedAt AS started_at, Source AS source, Status AS status,
                SessionJSON AS session_json FROM dbo.SessionArchives WHERE TestID=?""", session_id).fetchone()
            if row is None:
                return self._legacy_session(db, session_id)
            record = self.rows(cursor, [row])[0]
            return record, json.loads(record.pop("session_json"))

    def _legacy_session(self, db, session_id: str) -> tuple[dict, dict] | None:
        from emg_rehab.emg.session_analysis import add_point, new_session

        cursor = db.cursor()
        row = cursor.execute("""SELECT s.TestID, s.PatientID, COALESCE(s.Name, p.Name),
            COALESCE(s.Sex, p.Sex), s.CreatedAt, s.Status
            FROM dbo.Sessions s JOIN dbo.Patients p ON p.PatientID=s.PatientID
            WHERE s.TestID=?""", session_id).fetchone()
        if row is None:
            return None
        test_id, patient_id, name, gender, started_at, status = row
        document = new_session({"patientId": patient_id, "name": name, "gender": gender}, {})
        document["started_at"] = started_at.isoformat() if started_at else ""
        document["warnings"] = ["舊資料僅保留每秒彙整值；原始 CSV 未入庫，部分分析無法重建。"]
        emg = cursor.execute("""SELECT MuscleName, TimeOffsetSec, AvgRMSmV, BaseRMSmV, QualityStatus
            FROM dbo.EMGData WHERE TestID=? ORDER BY MuscleName, TimeOffsetSec""", session_id).fetchall()
        names = {name: index + 1 for index, name in enumerate(sorted({str(item[0]) for item in emg}))}
        document["channels"] = {str(slot): {"label": name, "mapping_name": name,
                                "sensor_serial": "Unknown SID"} for name, slot in names.items()}
        for muscle, time_s, rms, baseline, quality in emg:
            add_point(document, "emg", {"time_s": float(time_s or 0), "slot": names[str(muscle)],
                                        "rms_mv": rms, "quality": "有效" if quality == "VALID" else str(quality or "資料不足"),
                                        "base_rms_mv": baseline})
        pedal = cursor.execute("""SELECT TimeOffsetSec, TotalPowerW, CadenceRPM,
            LeftBalancePct, LeftPowerW, RightPowerW FROM dbo.PedalData
            WHERE TestID=? ORDER BY TimeOffsetSec""", session_id).fetchall()
        for time_s, power, cadence, balance, left, right in pedal:
            add_point(document, "pedal", {"time_s": float(time_s or 0), "power_w": power,
                                           "cadence_rpm": cadence, "left_balance_percent": balance,
                                           "left_power_w": left, "right_power_w": right})
        trainer = cursor.execute("""SELECT TimeOffsetSec, DistanceKm, SpeedKmh,
            PowerW, CadenceRPM FROM dbo.TrainerData WHERE TestID=? ORDER BY TimeOffsetSec""", session_id).fetchall()
        for time_s, distance, speed, power, cadence in trainer:
            add_point(document, "trainer", {"time_s": float(time_s or 0),
                                             "session_distance_meters": float(distance) * 1000 if distance is not None else None,
                                             "speed_kph": speed, "power_watts": power, "cadence_rpm": cadence})
        return ({"session_id": test_id, "patient_id": patient_id, "started_at": document["started_at"],
                 "source": "legacy", "status": status or "完成"}, document)

    def list_files(self, session_id: str) -> list[dict]:
        with self.connection() as db:
            cursor = db.cursor()
            values = cursor.execute("""SELECT Kind AS kind, FileName AS filename,
                DATALENGTH(Content) AS size FROM dbo.RawFiles WHERE TestID=? ORDER BY Kind""", session_id).fetchall()
            return self.rows(cursor, values)

    def get_file(self, session_id: str, kind: str) -> tuple[str, bytes] | None:
        with self.connection() as db:
            row = db.cursor().execute("SELECT FileName, Content FROM dbo.RawFiles WHERE TestID=? AND Kind=?",
                                      session_id, kind).fetchone()
            return (str(row[0]), bytes(row[1])) if row else None

    def delete_recording(self, session_id: str) -> None:
        with self.connection() as db:
            db.cursor().execute("DELETE FROM dbo.SessionArchives WHERE TestID=?", session_id)


_default = Archive()
list_patients = _default.list_patients
get_patient = _default.get_patient
save_patient = _default.save_patient
save_recording = _default.save_recording
list_sessions = _default.list_sessions
get_session = _default.get_session
list_files = _default.list_files
get_file = _default.get_file
delete_recording = _default.delete_recording
