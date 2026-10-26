"""Persistence adapter for the clinical SQL Server schema.

The web application and the desktop collector deliberately share the same
``Sessions``/telemetry tables.  CSV source files are stored outside SQL Server
and are referenced through the legacy spelling used by ``SourceFiles``.
"""
from __future__ import annotations

import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from werkzeug.utils import secure_filename

from modules.services.database import connect
from modules.services.analysis_cache import invalidate as invalidate_analysis_cache

ROOT = Path(__file__).resolve().parents[2]


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _muscle_key(value) -> str:
    """Make database muscle labels comparable across Qt and CSV records."""
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _second(value):
    value = _number(value)
    return None if value is None else max(0, min(32767, int(value)))


def _started_at(session: dict) -> datetime:
    try:
        value = datetime.fromisoformat(str(session.get("started_at", "")).replace("Z", "+00:00"))
    except ValueError:
        value = datetime.now(timezone.utc)
    return value.replace(tzinfo=None)


def _source_root() -> Path:
    path = ROOT / "data" / "source_files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _duration(session: dict) -> int | None:
    values = [_second(row.get("time_s")) for stream in ("emg", "pedal", "trainer") for row in session.get(stream, [])]
    return max((value for value in values if value is not None), default=None)


def _mean(rows: list[dict], field: str):
    values = [_number(row.get(field)) for row in rows]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def save(session: dict, files: dict[str, tuple[str, bytes]], *, source: str = "upload",
         test_id: str | None = None) -> str:
    patient_id = str((session.get("subject") or {}).get("patientId") or "").strip()
    if not patient_id:
        raise ValueError("A patient ID is required.")
    replacing = test_id is not None
    test_id, started_at = test_id or f"TEST-{uuid4()}", _started_at(session)
    target_folder = _source_root() / test_id
    if replacing and target_folder.resolve().parent != _source_root().resolve():
        raise ValueError("Invalid test storage path.")
    if replacing:
        invalidate_analysis_cache(test_id)
    folder = (_source_root() / f".staging-{uuid4()}" if replacing else target_folder)
    folder.mkdir()
    paths: dict[str, Path] = {}
    try:
        for kind, (filename, content) in files.items():
            path = folder / (secure_filename(filename) or f"{kind}.csv")
            path.write_bytes(content)
            paths[kind] = path
        pedal = session.get("pedal", [])
        left_balance = _mean(pedal, "left_balance_percent")
        trainer_distance = [_number(row.get("session_distance_meters")) for row in session.get("trainer", [])]
        trainer_distance = [value for value in trainer_distance if value is not None]
        with connect() as db:
            cursor = db.cursor()
            cursor.execute("SELECT 1 FROM dbo.Patients WHERE PatientID=%s", (patient_id,))
            if not cursor.fetchone():
                raise ValueError("The selected patient does not exist.")
            if replacing:
                cursor.execute("SELECT 1 FROM dbo.Sessions WHERE TestID=%s", (test_id,))
                if not cursor.fetchone():
                    raise ValueError("The selected test does not exist.")
                for table in ("EMGData", "PedalData", "TrainerData", "TestBaselines", "SourceFiles"):
                    cursor.execute(f"DELETE FROM dbo.{table} WHERE TestID=%s", (test_id,))
            subject = session.get("subject") or {}
            session_values = (patient_id, subject.get("name"), subject.get("gender"), "CSV Import", _duration(session),
                              bool(session.get("baselines")), "completed", _mean(pedal, "power_w"),
                              max((_number(row.get("power_w")) or 0 for row in pedal), default=None), _mean(pedal, "cadence_rpm"),
                              max(trainer_distance, default=None) / 1000 if trainer_distance else None, left_balance,
                              None if left_balance is None else 100 - left_balance, started_at)
            if replacing:
                cursor.execute("""UPDATE dbo.Sessions SET PatientID=%s, Name=%s, Sex=%s, ActionType=%s, TestTime=%s,
                    HasBaseline=%s, Status=%s, AvgPowerW=%s, MaxPowerW=%s, AvgCadenceRPM=%s, TotalDistanceKm=%s,
                    AvgLeftBalancePct=%s, AvgRightBalancePct=%s, CreatedAt=%s WHERE TestID=%s""", (*session_values, test_id))
            else:
                cursor.execute("""INSERT INTO dbo.Sessions (TestID, PatientID, Name, Sex, ActionType, TestTime,
                    HasBaseline, Status, AvgPowerW, MaxPowerW, AvgCadenceRPM, TotalDistanceKm,
                    AvgLeftBalancePct, AvgRightBalancePct, CreatedAt)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (test_id, *session_values))
            for slot, baseline in (session.get("baselines") or {}).items():
                cursor.execute("""INSERT INTO dbo.TestBaselines (BaseID, TestID, MuscleName, BaseTime, ActionType,
                    BaseRMSmV, YellowDeviationPct, RedDeviationPct, QualityStatus, CreatedAt)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (f"BASE-{uuid4()}", test_id, str(baseline.get("mapping_name") or f"S{slot}"), 0,
                     baseline.get("protocol"), _number(baseline.get("rms_mv")), 20, 40, "VALID", started_at))
            channels = session.get("channels") or {}
            for row in session.get("emg", []):
                second = _second(row.get("time_s"))
                if second is None: continue
                channel = channels.get(str(row.get("slot")), {})
                rms = _number(row.get("rms_mv"))
                cursor.execute("""INSERT INTO dbo.EMGData (EMGDataID, TestID, TimeOffsetSec, MuscleName, AvgRMSmV,
                    MaxRMSmV, BaseRMSmV, DeviationPct, AlertStatus, QualityStatus, RecordedAt)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (f"EMG-{uuid4()}", test_id, second, str(channel.get("mapping_name") or channel.get("label") or f"S{row.get('slot', '')}"),
                     rms, _number(row.get("rms_peak_mv")) or rms, _number(row.get("base_rms_mv")),
                     _number(row.get("deviation_pct")), row.get("alert_status"), str(row.get("quality") or "UNKNOWN")[:20],
                     started_at + timedelta(seconds=second)))
            for table, prefix, fields, stream in (
                ("PedalData", "PEDAL", ("TotalPowerW", "LeftPowerW", "RightPowerW", "LeftBalancePct", "RightBalancePct", "CadenceRPM"), "pedal"),
                ("TrainerData", "TRAINER", ("DistanceKm", "SpeedKmh", "PowerW", "CadenceRPM"), "trainer"),
            ):
                for row in session.get(stream, []):
                    second = _second(row.get("time_s"))
                    if second is None: continue
                    if stream == "pedal":
                        left = _number(row.get("left_balance_percent"))
                        values = (_number(row.get("power_w")), _number(row.get("left_power_w")), _number(row.get("right_power_w")), left, None if left is None else 100-left, _number(row.get("cadence_rpm")))
                    else:
                        distance = _number(row.get("session_distance_meters"))
                        values = (None if distance is None else distance/1000, _number(row.get("speed_kph")), _number(row.get("power_watts")), _number(row.get("cadence_rpm")))
                    identity = "PedalDataID" if table == "PedalData" else "TrainerDataID"
                    cursor.execute(f"INSERT INTO dbo.{table} ({identity}, TestID, TimeOffsetSec, {', '.join(fields)}, QualityStatus, RecordedAt) VALUES ({', '.join(['%s'] * (4 + len(fields) + 1))})",
                        (f"{prefix}-{uuid4()}", test_id, second, *values, str(row.get("quality") or "UNKNOWN")[:50], started_at + timedelta(seconds=second)))
            for kind, path in paths.items():
                cursor.execute("INSERT INTO dbo.SourceFiles (SouceID, TestID, Type, SoucePath) VALUES (%s,%s,%s,%s)",
                               (f"SOURCE-{uuid4()}", test_id, f"{source}:{kind}", str((target_folder / path.name).resolve())))
            db.commit()
        if replacing:
            managed_target = target_folder.resolve()
            shutil.rmtree(managed_target, ignore_errors=True)
            folder.replace(managed_target)
        return test_id
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def replace(test_id: str, session: dict, files: dict[str, tuple[str, bytes]]) -> str:
    """Replace one test's derived measurements with analysis of new raw files."""
    return save(session, files, source="upload", test_id=test_id)


def list_files(test_id: str) -> list[dict]:
    with connect() as db:
        cursor = db.cursor(); cursor.execute("SELECT Type, SoucePath FROM dbo.SourceFiles WHERE TestID=%s ORDER BY Type", (test_id,))
        result = []
        for file_type, raw_path in cursor.fetchall():
            path = Path(str(raw_path)); kind = str(file_type).rsplit(":", 1)[-1]
            result.append({"kind": kind, "filename": path.name, "size": path.stat().st_size if path.is_file() else 0})
        return result


def get_file(test_id: str, kind: str):
    with connect() as db:
        cursor = db.cursor(); cursor.execute("SELECT SoucePath FROM dbo.SourceFiles WHERE TestID=%s AND Type LIKE %s", (test_id, f"%:{kind}"))
        row = cursor.fetchone()
    if not row: return None
    path = Path(str(row[0])).resolve()
    try: path.relative_to(_source_root().resolve())
    except ValueError: return None
    return (path.name, path.read_bytes()) if path.is_file() else None


def delete(test_id: str) -> bool:
    """Remove one test and every dependent measurement; return False if absent.

    Source file paths are treated as untrusted database content: only files
    underneath the application's managed source directory may be removed.
    """
    with connect() as db:
        cursor = db.cursor()
        cursor.execute("SELECT SoucePath FROM dbo.SourceFiles WHERE TestID=%s", (test_id,))
        raw_paths = [Path(str(row[0])).resolve() for row in cursor.fetchall()]
        cursor.execute("SELECT 1 FROM dbo.Sessions WHERE TestID=%s", (test_id,))
        if not cursor.fetchone():
            return False
        try:
            for table in ("EMGData", "PedalData", "TrainerData", "TestBaselines", "SourceFiles"):
                cursor.execute(f"DELETE FROM dbo.{table} WHERE TestID=%s", (test_id,))
            cursor.execute("DELETE FROM dbo.Sessions WHERE TestID=%s", (test_id,))
            db.commit()
        except Exception:
            db.rollback()
            raise
    root = _source_root().resolve()
    for path in raw_paths:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent != root:
            try: parent.rmdir()
            except OSError: pass
    invalidate_analysis_cache(test_id)
    return True


def load_session(test_id: str):
    """Rebuild the analysis document from the normalized clinical tables."""
    # Imported lazily to keep database services independent from Flask imports.
    from modules.blueprints.analysis.emg.session_analysis import add_point, compare_rms, new_session
    with connect() as db:
        cursor = db.cursor()
        cursor.execute("SELECT TestID, PatientID, Name, Sex, CreatedAt, Status, TestTime FROM dbo.Sessions WHERE TestID=%s", (test_id,))
        session = cursor.fetchone()
        if not session: return None
        identifier, patient_id, name, sex, created_at, status, test_time = session
        cursor.execute("SELECT MuscleName, BaseRMSmV, ActionType, QualityStatus FROM dbo.TestBaselines WHERE TestID=%s", (test_id,))
        baseline_rows = cursor.fetchall()
        baseline_by_muscle = {_muscle_key(row[0]): row for row in baseline_rows if _number(row[1]) is not None}
        cursor.execute("""SELECT b.MuscleName, b.BaseRMSmV, baseline_test.CreatedAt
            FROM dbo.TestBaselines b JOIN dbo.Sessions baseline_test ON baseline_test.TestID=b.TestID
            WHERE baseline_test.PatientID=%s AND b.TestID<>%s AND baseline_test.CreatedAt<=%s
            ORDER BY baseline_test.CreatedAt DESC, b.CreatedAt DESC""",
            (patient_id, test_id, created_at or datetime.now()))
        prior_by_muscle = {}
        for muscle, value, recorded_at in cursor.fetchall():
            key = _muscle_key(muscle)
            if key not in prior_by_muscle and (_number(value) or 0) > 1e-9:
                prior_by_muscle[key] = {"rms_mv": _number(value),
                                        "recorded_at": recorded_at.isoformat() if recorded_at else ""}
        document = new_session({"patientId": patient_id, "name": name, "gender": sex}, {})
        document["started_at"] = created_at.isoformat() if created_at else ""
        cursor.execute("SELECT MuscleName, TimeOffsetSec, AvgRMSmV, MaxRMSmV, BaseRMSmV, QualityStatus FROM dbo.EMGData WHERE TestID=%s ORDER BY MuscleName, TimeOffsetSec", (test_id,))
        emg_rows = cursor.fetchall()
        muscles = {str(row[0]) for row in emg_rows} | {str(row[0]) for row in baseline_rows}
        slots = {label: index for index, label in enumerate(sorted(muscles), 1)}
        document["channels"] = {str(slot): {"label": name, "mapping_name": name} for name, slot in slots.items()}
        document["baselines"] = {
            str(slot): {"mapping_name": muscle, "rms_mv": _number(baseline_by_muscle[_muscle_key(muscle)][1]),
                        "protocol": baseline_by_muscle[_muscle_key(muscle)][2],
                        "quality": baseline_by_muscle[_muscle_key(muscle)][3]}
            for muscle, slot in slots.items() if _muscle_key(muscle) in baseline_by_muscle
        }
        document["prior_baselines"] = {
            str(slot): prior_by_muscle[_muscle_key(muscle)]
            for muscle, slot in slots.items() if _muscle_key(muscle) in prior_by_muscle
        }
        for muscle, time_s, rms, peak, base, quality in emg_rows:
            baseline = _number(base)
            comparison = compare_rms(_number(rms), baseline)
            # Qt writes BaseRMSmV only when the reference was compatible at
            # capture time.  A TestBaselines row can exist without being
            # applied, so it must never be substituted into this comparison.
            stored_quality = str(quality or "").strip()
            quality_labels = {
                "NO_BASELINE": "本次測試未套用基準" if (_muscle_key(muscle) in baseline_by_muscle or
                                                    _muscle_key(muscle) in prior_by_muscle) else "無基準值",
                "BASELINE_INCOMPATIBLE": "基準不相容",
                "BASELINE_TOO_LOW": "基準值過低",
                "INSUFFICIENT_DATA": "EMG 資料不足",
            }
            point_quality = (quality_labels.get(stored_quality.upper()) or
                             (comparison["quality"] if stored_quality.upper() in {"", "VALID", "UNKNOWN"} else stored_quality))
            if baseline is None and (_muscle_key(muscle) in baseline_by_muscle or
                                     _muscle_key(muscle) in prior_by_muscle) and point_quality == "無基準值":
                point_quality = "本次測試未套用基準"
            add_point(document, "emg", {"time_s": float(time_s or 0), "slot": slots[str(muscle)],
                "rms_mv": _number(rms), "rms_peak_mv": _number(peak), "base_rms_mv": baseline,
                **comparison, "quality": point_quality})
        cursor.execute("SELECT TimeOffsetSec, TotalPowerW, LeftPowerW, RightPowerW, LeftBalancePct, RightBalancePct, CadenceRPM, QualityStatus FROM dbo.PedalData WHERE TestID=%s ORDER BY TimeOffsetSec", (test_id,))
        for time_s, total, left, right, left_pct, right_pct, cadence, quality in cursor.fetchall():
            elapsed = float(time_s or 0); add_point(document, "pedal", {"time_s": elapsed, "timestamp": elapsed, "power_w": _number(total), "left_power_w": _number(left), "right_power_w": _number(right), "left_balance_percent": _number(left_pct), "cadence_rpm": _number(cadence), "quality": str(quality or "UNKNOWN")})
        cursor.execute("SELECT TimeOffsetSec, DistanceKm, SpeedKmh, PowerW, CadenceRPM, QualityStatus FROM dbo.TrainerData WHERE TestID=%s ORDER BY TimeOffsetSec", (test_id,))
        for time_s, distance, speed, power, cadence, quality in cursor.fetchall():
            elapsed = float(time_s or 0); add_point(document, "trainer", {"time_s": elapsed, "session_distance_meters": None if distance is None else _number(distance)*1000, "speed_kph": _number(speed), "power_watts": _number(power), "cadence_rpm": _number(cadence), "quality": str(quality or "UNKNOWN")})
    return ({"session_id": identifier, "patient_id": patient_id, "patient_name": name, "started_at": document["started_at"], "source": "upload" if list_files(test_id) else "qt", "status": status or "completed", "duration_seconds": _number(test_time)}, document)


def list_sessions(patient_id: str | None = None) -> list[dict]:
    with connect() as db:
        cursor = db.cursor()
        query = """SELECT s.TestID AS session_id, s.PatientID AS patient_id, COALESCE(s.Name, p.Name) AS name,
            CONVERT(nvarchar(64), s.CreatedAt, 126) AS started_at, COALESCE(s.Status, N'completed') AS status,
            s.TestTime AS duration_seconds, (SELECT COUNT(*) FROM dbo.SourceFiles f WHERE f.TestID=s.TestID) AS file_count,
            (SELECT COUNT(*) FROM dbo.EMGData e WHERE e.TestID=s.TestID) AS emg_count,
            (SELECT COUNT(*) FROM dbo.PedalData e WHERE e.TestID=s.TestID) AS pedal_count,
            (SELECT COUNT(*) FROM dbo.TrainerData e WHERE e.TestID=s.TestID) AS trainer_count,
            (SELECT COUNT(*) FROM dbo.TestBaselines b WHERE b.TestID=s.TestID) AS baseline_count
            FROM dbo.Sessions s LEFT JOIN dbo.Patients p ON p.PatientID=s.PatientID"""
        cursor.execute(query + (" WHERE s.PatientID=%s" if patient_id else "") + " ORDER BY s.CreatedAt DESC", (patient_id,) if patient_id else ())
        keys = [column[0] for column in cursor.description]
        return [{**dict(zip(keys, row)), "source": "upload" if row[6] else "qt"} for row in cursor.fetchall()]
