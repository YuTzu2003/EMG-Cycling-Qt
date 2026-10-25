"""CSV upload, session analysis, comparison, and download routes."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from uuid import uuid4
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from markupsafe import Markup
from werkzeug.utils import secure_filename
from modules.services.database import connect as _connection
from modules.services.file_storage import temporary_files
from modules.blueprints.analysis.emg.sensor_mapping import loadMappings, recordingChannelSides
from modules.blueprints.analysis.emg.importers import attach_pedal_csv, import_emg_csv
from modules.blueprints.analysis.pedal.angle_emg import AngleSettings, analyze_angle_emg
from modules.blueprints.analysis.pedal.analysis import analyze_pedal_records, summarize_pedal_records
from modules.blueprints.analysis.emg.session_analysis import add_point, analyze, new_session, peak_envelope, summary_lines
from modules.blueprints.analysis.emg.session_compare import compare_sessions, comparison_charts
from modules.blueprints.analysis.emg.visuals import angle_profile_charts
from modules.blueprints.analysis.pdf_report import build_session_pdf

ROOT = Path(__file__).resolve().parents[3]
analysis_bp = Blueprint("analysis", __name__, template_folder="templates")

def _rows(cursor, values):
    return [dict(zip([column[0] for column in cursor.description], row)) for row in values]

def _db_number(value):
    return None if value is None else float(value)


def _list_patients():
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT PatientID AS patient_id, Name AS name, Sex AS gender, Notes AS notes FROM dbo.Patients ORDER BY Name, PatientID")
        return _rows(cursor, cursor.fetchall())

def _get_patient(patient_id):
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT PatientID AS patient_id, Name AS name, Sex AS gender, Notes AS notes FROM dbo.Patients WHERE PatientID=%s", (patient_id,))
        row = cursor.fetchone(); return _rows(cursor, [row])[0] if row else None

def _list_sessions(patient_id=None):
    with _connection() as db:
        cursor = db.cursor()
        archived = """SELECT a.TestID AS session_id, a.PatientID AS patient_id, p.Name AS name,
            a.StartedAt AS started_at, a.Source AS source, a.Status AS status,
            (SELECT COUNT(*) FROM dbo.RawFiles f WHERE f.TestID=a.TestID) AS file_count,
            0 AS emg_count, 0 AS pedal_count, 0 AS trainer_count,
            COALESCE(TRY_CONVERT(float, JSON_VALUE(a.SessionJSON, '$.duration_s')),
                     duration.duration_s) AS duration_seconds
            FROM dbo.SessionArchives a JOIN dbo.Patients p ON p.PatientID=a.PatientID
            OUTER APPLY (
                SELECT MAX(TRY_CONVERT(float, JSON_VALUE(sample.[value], '$.time_s'))) AS duration_s
                FROM (
                    SELECT [value] FROM OPENJSON(a.SessionJSON, '$.emg')
                    UNION ALL SELECT [value] FROM OPENJSON(a.SessionJSON, '$.pedal')
                    UNION ALL SELECT [value] FROM OPENJSON(a.SessionJSON, '$.trainer')
                ) sample
            ) duration"""
        cursor.execute(archived + (" WHERE a.PatientID=%s" if patient_id else ""),
                       (patient_id,) if patient_id else ())
        records = _rows(cursor, cursor.fetchall())
        legacy = """SELECT s.TestID AS session_id, s.PatientID AS patient_id,
            COALESCE(s.Name, p.Name) AS name, CONVERT(nvarchar(64), s.CreatedAt, 126) AS started_at,
            N'qt' AS source, COALESCE(s.Status, N'completed') AS status, 0 AS file_count,
            (SELECT COUNT(*) FROM dbo.EMGData e WHERE e.TestID=s.TestID) AS emg_count,
            (SELECT COUNT(*) FROM dbo.PedalData pd WHERE pd.TestID=s.TestID) AS pedal_count,
            (SELECT COUNT(*) FROM dbo.TrainerData td WHERE td.TestID=s.TestID) AS trainer_count,
            s.TestTime AS duration_seconds
            FROM dbo.Sessions s LEFT JOIN dbo.Patients p ON p.PatientID=s.PatientID
            WHERE NOT EXISTS (SELECT 1 FROM dbo.SessionArchives a WHERE a.TestID=s.TestID)"""
        cursor.execute(legacy + (" AND s.PatientID=%s" if patient_id else ""),
                       (patient_id,) if patient_id else ())
        records.extend(_rows(cursor, cursor.fetchall()))
        return sorted(records, key=lambda item: item["started_at"] or "", reverse=True)

def _save_recording(session, files, *, source="upload"):
    import json
    from datetime import datetime, timezone
    patient_id = str((session.get("subject") or {}).get("patientId") or "").strip()
    identifier = f"TEST-{uuid4()}"
    if not patient_id: raise ValueError("測試紀錄缺少病人 ID。")
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT 1 FROM dbo.Patients WHERE PatientID=%s", (patient_id,))
        if not cursor.fetchone(): raise ValueError("找不到病人資料。")
        cursor.execute("INSERT INTO dbo.SessionArchives (TestID, PatientID, StartedAt, Source, Status, SessionJSON) VALUES (%s,%s,%s,%s,%s,%s)", (identifier, patient_id, str(session.get("started_at") or datetime.now(timezone.utc).isoformat()), source, "完成", json.dumps(session, ensure_ascii=False, allow_nan=False)))
        for kind, value in files.items():
            filename, content = value
            cursor.execute("INSERT INTO dbo.RawFiles (TestID, Kind, FileName, Content) VALUES (%s,%s,%s,%s)", (identifier, kind, filename, bytearray(content)))
        db.commit()
    return identifier

def _get_session(session_id):
    with _connection() as db:
        import json
        cursor = db.cursor(); cursor.execute("""SELECT a.TestID AS session_id, a.PatientID AS patient_id,
            p.Name AS patient_name, a.StartedAt AS started_at, a.Source AS source, a.Status AS status,
            a.SessionJSON AS session_json FROM dbo.SessionArchives a
            LEFT JOIN dbo.Patients p ON p.PatientID=a.PatientID WHERE a.TestID=%s""", (session_id,))
        row = cursor.fetchone()
        if row:
            record = _rows(cursor, [row])[0]
            return record, json.loads(record.pop("session_json"))
        cursor.execute("SELECT TestID, PatientID, Name, Sex, CreatedAt, Status FROM dbo.Sessions WHERE TestID=%s", (session_id,))
        legacy = cursor.fetchone()
        if not legacy:
            return None
        test_id, patient_id, name, gender, started_at, status = legacy
        document = new_session({"patientId": patient_id, "name": name, "gender": gender}, {})
        document["started_at"] = started_at.isoformat() if started_at else ""
        cursor.execute("SELECT MuscleName, TimeOffsetSec, AvgRMSmV, MaxRMSmV, BaseRMSmV, QualityStatus FROM dbo.EMGData WHERE TestID=%s ORDER BY MuscleName, TimeOffsetSec", (session_id,))
        emg_rows = cursor.fetchall()
        slots = {str(label): index + 1 for index, label in enumerate(sorted({str(row[0]) for row in emg_rows}))}
        document["channels"] = {str(slot): {"label": label, "mapping_name": label} for label, slot in slots.items()}
        for muscle, time_s, rms, peak_rms, base_rms, quality in emg_rows:
            add_point(document, "emg", {"time_s": float(time_s or 0), "slot": slots[str(muscle)],
                "rms_mv": _db_number(rms), "rms_peak_mv": _db_number(peak_rms),
                "base_rms_mv": _db_number(base_rms), "quality": str(quality or "UNKNOWN")})
        # The Qt collector stores the pedal and trainer streams in their own
        # tables.  Recreate the same in-memory recording that CSV imports use,
        # so every existing Qt test can be analysed by the website.
        cursor.execute("""SELECT TimeOffsetSec, TotalPowerW, LeftPowerW, RightPowerW,
            LeftBalancePct, RightBalancePct, CadenceRPM, QualityStatus
            FROM dbo.PedalData WHERE TestID=%s ORDER BY TimeOffsetSec""", (session_id,))
        for time_s, total, left, right, left_balance, right_balance, cadence, quality in cursor.fetchall():
            elapsed = float(time_s or 0)
            add_point(document, "pedal", {"time_s": elapsed, "timestamp": elapsed,
                "power_w": _db_number(total), "left_power_w": _db_number(left),
                "right_power_w": _db_number(right),
                "left_balance_percent": _db_number(left_balance),
                "right_balance_percent": _db_number(right_balance),
                "cadence_rpm": _db_number(cadence), "quality": str(quality or "UNKNOWN")})
        cursor.execute("""SELECT TimeOffsetSec, DistanceKm, SpeedKmh, PowerW, CadenceRPM, QualityStatus
            FROM dbo.TrainerData WHERE TestID=%s ORDER BY TimeOffsetSec""", (session_id,))
        for time_s, distance, speed, power, cadence, quality in cursor.fetchall():
            add_point(document, "trainer", {"time_s": float(time_s or 0),
                "session_distance_meters": None if distance is None else _db_number(distance) * 1000,
                "speed_kph": _db_number(speed), "power_watts": _db_number(power),
                "cadence_rpm": _db_number(cadence),
                "quality": str(quality or "UNKNOWN")})
        return ({"session_id": test_id, "patient_id": patient_id, "patient_name": name, "started_at": document["started_at"],
                 "source": "qt", "status": status or "completed"}, document)

def _list_files(session_id):
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT Kind AS kind, FileName AS filename, DATALENGTH(Content) AS size FROM dbo.RawFiles WHERE TestID=%s ORDER BY Kind", (session_id,)); return _rows(cursor, cursor.fetchall())

def _get_file(session_id, kind):
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT FileName, Content FROM dbo.RawFiles WHERE TestID=%s AND Kind=%s", (session_id, kind)); row=cursor.fetchone(); return (str(row[0]), bytes(row[1])) if row else None


def _emg_chart_series(document: dict) -> list[dict]:
    """Prepare JSON-safe EMG RMS series for the browser chart."""
    grouped: dict[str, list[list[float]]] = {}
    for point in document.get("emg", []):
        try:
            time_s, rms = float(point["time_s"]), float(point["rms_mv"])
        except (KeyError, TypeError, ValueError):
            continue
        slot = str(point.get("slot", ""))
        channel = document.get("channels", {}).get(slot, {})
        name = channel.get("mapping_name") or channel.get("label") or f"S{slot}"
        grouped.setdefault(str(name), []).append([time_s, rms])
    return [{"name": name, "data": values[::max(1, len(values) // 1200)]}
            for name, values in sorted(grouped.items())]


def _emg_ride_charts(document: dict, result: dict) -> tuple[list[dict], list[dict]]:
    """Match the Qt ride view: RMS mean/peak bars and one-second peak envelopes."""
    channels = document.get("channels", {})
    bars: list[dict] = []
    envelopes: list[dict] = []
    for slot in sorted(result.get("emg", {}), key=lambda value: int(value) if str(value).isdigit() else str(value)):
        stats = result["emg"][slot].get("rms_mv", {})
        label = channels.get(str(slot), {}).get("label") or channels.get(str(slot), {}).get("mapping_name") or f"S{slot}"
        display = f"S{int(slot):02d} {label}" if str(slot).isdigit() else str(label)
        rows = [row for row in document.get("emg", []) if str(row.get("slot")) == str(slot)]
        recorded_peaks = [_db_number(row.get("rms_peak_mv")) for row in rows if row.get("rms_peak_mv") is not None]
        bars.append({"label": display, "mean": stats.get("mean"),
                     "peak": max(recorded_peaks) if recorded_peaks else stats.get("max")})
        points = peak_envelope([{**row, "rms_mv": row.get("rms_peak_mv") or row.get("rms_mv")} for row in rows])
        stride = max(1, len(points) // 2000)
        envelopes.append({"slot": str(slot), "label": display,
                          "data": [[time_s, value] for time_s, value in points[::stride]]})
    return bars, envelopes


def _telemetry_chart_series(document: dict, stream: str, field: str, name: str) -> list[dict]:
    """Return a down-sampled ECharts time series from a recorded stream."""
    values: list[list[float]] = []
    for point in document.get(stream, []):
        try:
            values.append([float(point["time_s"]), float(point[field])])
        except (KeyError, TypeError, ValueError):
            continue
    stride = max(1, len(values) // 1200)
    return [{"name": name, "data": values[::stride]}] if values else []


def _pedal_chart_series(performance: dict | None) -> tuple[list[dict], list[dict], list[dict]]:
    """Format the same pedal time series used by the Qt summary workspace."""
    rows = (performance or {}).get("series", [])
    def series(field: str, name: str) -> list[dict]:
        points = [[row["elapsed_s"], row[field]] for row in rows if row.get(field) is not None]
        return [{"name": name, "data": points}] if points else []
    balance_left = [[row["elapsed_s"], row["left_balance_percent"]]
                    for row in rows if row.get("left_balance_percent") is not None]
    balance = ([{"name": "左側", "data": balance_left},
                {"name": "右側", "data": [[time_s, 100 - value] for time_s, value in balance_left]}]
               if balance_left else [])
    return series("power_w", "功率"), series("cadence_rpm", "踏頻"), balance





def window_comparison(first: dict, second: dict, early_s: int, late_s: int) -> list[dict]:
    """Calculate each muscle's early and late RMS windows for two sessions."""
    def grouped(document: dict) -> dict[str, list[dict]]:
        channels = document.get("channels", {})
        values: dict[str, list[dict]] = {}
        for point in document.get("emg", []):
            slot = str(point.get("slot", ""))
            label = channels.get(slot, {}).get("mapping_name") or f"S{slot}"
            values.setdefault(label, []).append(point)
        return values

    def average(points: list[dict], start: float, end: float) -> float | None:
        values = [float(row["rms_mv"]) for row in points if row.get("rms_mv") is not None
                  and start <= float(row["time_s"]) <= end]
        return sum(values) / len(values) if values else None

    a, b = grouped(first), grouped(second)
    rows = []
    for label in sorted(set(a) | set(b)):
        pair = []
        for points in (a.get(label, []), b.get(label, [])):
            end = max((float(row["time_s"]) for row in points), default=-1)
            pair.append((average(points, 0, early_s - 1) if end + 1 >= early_s else None,
                         average(points, max(0, end - late_s + 1), end) if end + 1 >= late_s else None))
        rows.append({"label": label, "a_early": pair[0][0], "a_late": pair[0][1],
                     "b_early": pair[1][0], "b_late": pair[1][1]})
    return rows


def _analysis_sections(document: dict, result: dict, pedal_performance: dict | None, source_kinds: set[str]) -> list[dict]:
    """Describe every analysis available to the session-detail user interface."""
    has_angle_source = {"emg", "pedal"} <= source_kinds or {"emg", "angle"} <= source_kinds
    return [
        {"key": "emg", "title": "EMG 肌電分析", "description": "RMS、前後段變化、訊號品質與肌肉通道摘要。",
         "available": bool(result.get("emg")), "count": len(result.get("emg", {}))},
        {"key": "pedal", "title": "踏板動力分析", "description": "功率、迴轉速、左右平衡與踩踏圈數。",
         "available": bool(result.get("pedal")), "count": len(document.get("pedal", []))},
        {"key": "trainer", "title": "訓練器資料分析", "description": "阻力、速度、心率、距離與熱量。",
         "available": bool(result.get("trainer")), "count": len(document.get("trainer", []))},
        {"key": "load", "title": "負荷與同步關係", "description": "EMG 與負荷的 Pearson 相關、阻力區段與資料對齊。",
         "available": any(value is not None for value in result.get("load_relationship", {}).values()), "count": 0},
        {"key": "performance", "title": "踏板效能指標", "description": "功率分布、訓練區間與進階踏板表現。",
         "available": pedal_performance is not None, "count": 0},
        {"key": "angle", "title": "曲柄角度活化", "description": "依曲柄角度比較肌肉啟動區間、曲線與踩踏圈。",
         "available": has_angle_source, "count": 0},
        {"key": "compare", "title": "跨測試比較", "description": "選擇同一病人的兩次測試，檢視 RMS、雙側與時間窗變化。",
         "available": True, "count": 0},
    ]


def _pedal_performance_lines(performance: dict) -> list[str]:
    def value(key: str, unit: str, digits: int = 1) -> str:
        number = performance.get(key)
        return "—" if number is None else f"{float(number):.{digits}f} {unit}"

    return [
        f"累積功：{value('energy_kj', 'kJ')}",
        f"最高平均功率：5 秒 {value('peak_5s_w', 'W')}；60 秒 {value('peak_60s_w', 'W')}",
        f"資料涵蓋率：{value('coverage_percent', '%', 0)}（缺少 {performance.get('missing_seconds', 0)} 秒）",
        f"滑行時間：{performance.get('coasting_seconds', 0)} 秒",
    ]








@analysis_bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        patient = _get_patient(request.form.get("patient_id", ""))
        if patient is None:
            flash("請先選擇已建立的病人。", "danger")
            return redirect(url_for("analysis.upload"))
        uploads = {kind: request.files.get(kind) for kind in ("emg", "pedal", "trainer", "angle")}
        uploads = {kind: file for kind, file in uploads.items() if file and file.filename}
        if not uploads or any(Path(file.filename).suffix.lower() != ".csv" for file in uploads.values()):
            flash("請至少上傳一個 CSV 檔案。", "danger")
            return redirect(url_for("analysis.upload"))
        try:
            with temporary_files() as temp_path:
                paths = {}
                for kind, file in uploads.items():
                    filename = secure_filename(file.filename) or f"{kind}.csv"
                    path = temp_path(f"{kind}_{filename}")
                    file.save(path)
                    paths[kind] = path
                subject = {"patientId": patient["patient_id"], "name": patient["name"], "gender": patient["gender"]}
                recording = (import_emg_csv(paths["emg"], loadMappings(ROOT / "sensor_mapping.json"), subject)
                             if "emg" in paths else new_session(subject, {}))
                if "pedal" in paths:
                    attach_pedal_csv(recording, paths["pedal"])
                if "trainer" in paths:
                    with paths["trainer"].open(encoding="utf-8-sig", newline="") as stream:
                        reader = csv.DictReader(stream)
                        if "time_seconds" not in (reader.fieldnames or []):
                            raise ValueError("訓練器 CSV 必須含有 time_seconds 欄位。")
                        for row in reader:
                            point = {key: (float(value) if value else None) for key, value in row.items()}
                            add_point(recording, "trainer", {"time_s": point.pop("time_seconds"), **point})
                recording["raw_files"] = {kind: uploads[kind].filename for kind in uploads}
                files = {kind: (uploads[kind].filename, path.read_bytes()) for kind, path in paths.items()}
                identifier = _save_recording(recording, files, source="upload")
            flash("CSV 已匯入並建立分析測試。", "success")
            return redirect(url_for("analysis.session_detail", session_id=identifier))
        except (ValueError, OSError, TypeError, OverflowError) as exc:
            flash(f"匯入失敗：{exc}", "danger")
    return render_template("upload.html", patients=_list_patients())


@analysis_bp.get("/sessions/<session_id>")
def session_detail(session_id: str):
    found = _get_session(session_id)
    if found is None:
        abort(404)
    record, document = found
    selected_view = request.args.get("view", "pedal")
    if selected_view not in {"pedal", "emg", "angle", "files"}:
        abort(404)
    angle_sections = (
        ("overview", "分析重點"), ("fatigue", "疲勞摘要"),
        ("fatigue_trend", "逐圈疲勞趨勢"), ("muscle", "肌肉曲線"),
        ("phase", "指定角度"), ("heatmap", "全肌肉熱圖"),
        ("profiles", "肌肉角度曲線"), ("polar", "圓形角度圖"),
        ("cycle8", "逐圈 8 角度"), ("comparison", "前後段數值"),
        ("values", "前後段表格"), ("interpretation", "數值與判讀"),
    )
    angle_section = request.args.get("angle_section", "overview")
    if angle_section not in {key for key, _ in angle_sections}:
        abort(404)
    result = analyze(document)
    pedal_performance = None
    pedal_summary = None
    performance_lines: list[str] = []
    if document.get("pedal"):
        try:
            # FTP is only needed for training-zone calculations. It remains
            # explicit until a per-patient FTP field is available.
            pedal_performance = analyze_pedal_records(document["pedal"], ftp_w=200)
            pedal_summary = summarize_pedal_records(document["pedal"])
            performance_lines = _pedal_performance_lines(pedal_performance)
        except (ValueError, KeyError, TypeError):
            # Old captures may not retain every timestamp field required by this optional analysis.
            performance_lines = ["此筆踏板原始資料缺少進階效能分析所需欄位。"]
    files = _list_files(session_id)
    source_kinds = {item["kind"] for item in files}
    emg_bars, emg_envelopes = _emg_ride_charts(document, result)
    emg_peak_by_slot = {row["label"].split(" ", 1)[0]: row["peak"] for row in emg_bars}
    pedal_power_series, pedal_cadence_series, pedal_balance_series = _pedal_chart_series(pedal_performance)
    angle_result = None
    angle_tables = None
    angle_error = None
    if {"emg", "pedal"} <= source_kinds or {"emg", "angle"} <= source_kinds:
        try:
            with temporary_files() as temp_path:
                emg_path = temp_path("emg.csv")
                pedal_path = temp_path("pedal.csv")
                emg_path.write_bytes(_get_file(session_id, "emg")[1])
                pedal_path.write_bytes(_get_file(session_id, "angle" if "angle" in source_kinds else "pedal")[1])
                with emg_path.open(encoding="utf-8-sig", newline="") as stream:
                    headers = next(csv.reader(stream), [])
                sides, _ = recordingChannelSides(
                    emg_path, headers, loadMappings(ROOT / "sensor_mapping.json"),
                    document.get("channels", {}),
                )
                angle_result = analyze_angle_emg(emg_path, pedal_path, sides, AngleSettings())
            angle_tables = {
                key: Markup(angle_result[key].to_html(index=False, classes="table table-sm data-table", border=0))
                for key in ("summary", "profiles", "cycles")
            }
            angle_result["profile_charts"] = angle_profile_charts(angle_result)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            angle_error = str(exc)
    emg_channel_names = {
        str(slot): str(channel.get("label") or channel.get("mapping_name") or f"S{slot}")
        for slot, channel in document.get("channels", {}).items()
    }
    chart_payload = {
        "emg_bars": emg_bars,
        "emg_envelopes": emg_envelopes,
        "pedal_power_series": pedal_power_series,
        "pedal_cadence_series": pedal_cadence_series,
        "pedal_balance_series": pedal_balance_series,
        "average_power_w": pedal_summary.get("average_power_w") if pedal_summary else None,
        "maximum_power_w": pedal_summary.get("maximum_power_w") if pedal_summary else None,
        "average_cadence_rpm": pedal_summary.get("average_cadence_rpm") if pedal_summary else None,
        "maximum_cadence_rpm": pedal_summary.get("maximum_cadence_rpm") if pedal_summary else None,
    }
    return render_template("session_detail.html", record=record, files=files, analysis=result,
                           selected_view=selected_view, angle_sections=angle_sections, angle_section=angle_section,
                           lines=summary_lines(result),
                           analysis_sections=_analysis_sections(document, result, pedal_performance, source_kinds),
                           pedal_performance=pedal_performance, performance_lines=performance_lines,
                           emg_chart_series=_emg_chart_series(document),
                           pedal_summary=pedal_summary,
                           pedal_power_series=pedal_power_series,
                           pedal_cadence_series=pedal_cadence_series,
                           pedal_balance_series=pedal_balance_series,
                           trainer_power_series=_telemetry_chart_series(document, "trainer", "power_watts", "訓練器功率"),
                           emg_bars=emg_bars, emg_envelopes=emg_envelopes, chart_payload=chart_payload,
                           emg_peak_by_slot=emg_peak_by_slot,
                           emg_sample_count=len(document.get("emg", [])),
                           pedal_sample_count=len(document.get("pedal", [])),
                           trainer_sample_count=len(document.get("trainer", [])),
                           emg_channel_names=emg_channel_names, angle_result=angle_result,
                           angle_tables=angle_tables, angle_error=angle_error)


@analysis_bp.get("/sessions/<session_id>/report.pdf")
def download_pdf_report(session_id: str):
    found = _get_session(session_id)
    if found is None:
        abort(404)
    record, document = found
    result = analyze(document)
    files = _list_files(session_id)
    pedal_summary = pedal_performance = None
    if document.get("pedal"):
        try:
            pedal_summary = summarize_pedal_records(document["pedal"])
            pedal_performance = analyze_pedal_records(document["pedal"], ftp_w=200)
        except (ValueError, KeyError, TypeError):
            pass
    angle_result = None
    angle_error = None
    kinds = {item["kind"] for item in files}
    if {"emg", "pedal"} <= kinds or {"emg", "angle"} <= kinds:
        try:
            with temporary_files() as temp_path:
                emg_path, pedal_path = temp_path("emg.csv"), temp_path("pedal.csv")
                emg_path.write_bytes(_get_file(session_id, "emg")[1])
                pedal_path.write_bytes(_get_file(session_id, "angle" if "angle" in kinds else "pedal")[1])
                with emg_path.open(encoding="utf-8-sig", newline="") as stream:
                    headers = next(csv.reader(stream), [])
                settings = AngleSettings()
                if request.args.get("angle_export") == "1":
                    with emg_path.open(encoding="utf-8-sig", newline="") as stream:
                        emg_duration = max((float(row.get("time_seconds", 0) or 0)
                                            for row in csv.DictReader(stream)), default=0.0)
                    with pedal_path.open(encoding="utf-8-sig", newline="") as stream:
                        reader = csv.DictReader(stream)
                        time_column = "t_session_s" if "t_session_s" in (reader.fieldnames or ()) else "session_seconds"
                        pedal_duration = max((float(row.get(time_column, 0) or 0)
                                              for row in reader), default=0.0)
                    duration = min(emg_duration, pedal_duration)
                    if duration < 35:
                        raise ValueError("EMG 與踏板共同資料不足 35 秒，無法建立前後段角度分析。")
                    if request.args.get("automatic_range") == "on":
                        window = min(60.0, max(10.0, (duration - 5.0) / 3.0))
                        start = (5.0, round(5.0 + window, 2))
                        end = (round(duration - window, 2), round(duration, 2))
                    else:
                        start = (float(request.args.get("start_a", 5)), float(request.args.get("start_b", 65)))
                        end = (float(request.args.get("end_a", 240)), float(request.args.get("end_b", 300)))
                    settings = AngleSettings(
                        start=start, end=end, bins=int(request.args.get("bins", 24)),
                        percentile=float(request.args.get("percentile", 99)),
                        scale=None if request.args.get("auto_scale") == "on" else float(request.args.get("scale", 1)),
                        offset_s=None if request.args.get("auto_offset") == "on" else float(request.args.get("offset_s", 0)),
                    )
                    settings.validate()
                sides, _ = recordingChannelSides(
                    emg_path, headers, loadMappings(ROOT / "sensor_mapping.json"),
                    document.get("channels", {}),
                )
                angle_result = analyze_angle_emg(emg_path, pedal_path, sides, settings)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            angle_error = str(exc)
    try:
        pdf = build_session_pdf(record, document, result, pedal_summary=pedal_summary,
                                pedal_performance=pedal_performance, files=files,
                                angle_result=angle_result, angle_error=angle_error)
    except RuntimeError as exc:
        abort(503, str(exc))
    filename = secure_filename(f"rehab-analysis-{record['patient_id']}-{session_id}.pdf") or "rehab-analysis.pdf"
    response = send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                         download_name=filename)
    response.headers["Cache-Control"] = "no-store"
    return response


@analysis_bp.get("/sessions/<session_id>/source-data")
def source_data(session_id: str):
    found = _get_session(session_id)
    if found is None:
        abort(404)
    record, document = found
    result = analyze(document)
    return render_template("source_data.html", record=record, analysis=result,
                           files=_list_files(session_id),
                           emg_rows=len(document.get("emg", [])),
                           pedal_rows=len(document.get("pedal", [])),
                           trainer_rows=len(document.get("trainer", [])))


@analysis_bp.get("/sessions/<session_id>/files/<kind>")
def download_file(session_id: str, kind: str):
    found = _get_file(session_id, kind)
    if found is None:
        abort(404)
    return send_file(io.BytesIO(found[1]), mimetype="text/csv" if kind != "info" else "text/plain",
                     as_attachment=True, download_name=secure_filename(found[0]) or f"{kind}.csv")


@analysis_bp.route("/compare", methods=["GET", "POST"])
def compare():
    all_patients = _list_patients()
    patient_id = request.values.get("patient_id", "")
    choices = _list_sessions(patient_id) if patient_id else []
    result = None
    window_rows: list[dict] = []
    charts = None
    early_s, late_s = 30, 30
    if request.method == "POST":
        try:
            early_s, late_s = int(request.form.get("early_s", 30)), int(request.form.get("late_s", 30))
            if not (1 <= early_s <= 600 and 1 <= late_s <= 600):
                raise ValueError
        except ValueError:
            abort(400, "時間窗必須介於 1 到 600 秒。")
        first, second = _get_session(request.form.get("first", "")), _get_session(request.form.get("second", ""))
        if not first or not second or first[0]["patient_id"] != patient_id or second[0]["patient_id"] != patient_id:
            flash("請選擇同一位病人的兩筆不同測試。", "danger")
        elif first[0]["session_id"] == second[0]["session_id"]:
            flash("請選擇兩筆不同測試。", "danger")
        else:
            result = compare_sessions(first[1], second[1])
            window_rows = window_comparison(first[1], second[1], early_s, late_s)
            charts = Markup(comparison_charts(first[1], second[1], result))
    chart_rows = ([{"label": row["label"], "first": row["first"]["median_mv"],
                    "second": row["second"]["median_mv"]} for row in result["channels"]
                   if row["first"]["median_mv"] is not None and row["second"]["median_mv"] is not None]
                  if result else [])
    return render_template("compare.html", patients=all_patients, patient_id=patient_id, choices=choices, result=result,
                           window_rows=window_rows, charts=charts, early_s=early_s, late_s=late_s,
                           comparison_chart_rows=chart_rows)


@analysis_bp.route("/sessions/<session_id>/angle", methods=["GET", "POST"])
def angle(session_id: str):
    record = _get_session(session_id)
    if record is None:
        abort(404)
    available = _list_files(session_id)
    kinds = {file["kind"] for file in available}
    result = tables = None
    source_ready = {"emg", "pedal"} <= kinds or {"emg", "angle"} <= kinds
    values = {
        "automatic_range": request.form.get("automatic_range") == "on" if request.method == "POST" else True,
        "start_a": request.form.get("start_a", "5"), "start_b": request.form.get("start_b", "65"),
        "end_a": request.form.get("end_a", "240"), "end_b": request.form.get("end_b", "300"),
        "auto_scale": request.form.get("auto_scale") == "on" if request.method == "POST" else True,
        "scale": request.form.get("scale", "1.0"),
        "auto_offset": request.form.get("auto_offset") == "on" if request.method == "POST" else False,
        "offset_s": request.form.get("offset_s", "0.0"),
        "bins": request.form.get("bins", "24"), "percentile": request.form.get("percentile", "99"),
    }
    error = None
    database_result = None
    if source_ready:
        try:
            with temporary_files() as temp_path:
                emg, pedal = temp_path("emg.csv"), temp_path("pedal.csv")
                emg.write_bytes(_get_file(session_id, "emg")[1])
                pedal.write_bytes(_get_file(session_id, "angle" if "angle" in kinds else "pedal")[1])
                with emg.open(encoding="utf-8-sig", newline="") as stream:
                    headers = next(csv.reader(stream), [])
                    stream.seek(0)
                    reader = csv.DictReader(stream)
                    emg_duration = max((float(row.get("time_seconds", 0) or 0) for row in reader), default=0.0)
                with pedal.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    time_column = "t_session_s" if "t_session_s" in (reader.fieldnames or ()) else "session_seconds"
                    pedal_duration = max((float(row.get(time_column, 0) or 0) for row in reader), default=0.0)
                duration = min(emg_duration, pedal_duration)
                if duration < 35:
                    raise ValueError("EMG 與踏板共同資料不足 35 秒，無法建立前後段角度分析。")
                if values["automatic_range"]:
                    window = min(60.0, max(10.0, (duration - 5.0) / 3.0))
                    start, end = (5.0, round(5.0 + window, 2)), (round(duration - window, 2), round(duration, 2))
                else:
                    start = (float(values["start_a"]), float(values["start_b"]))
                    end = (float(values["end_a"]), float(values["end_b"]))
                settings = AngleSettings(start=start, end=end, bins=int(values["bins"]),
                                         percentile=float(values["percentile"]),
                                         scale=None if values["auto_scale"] else float(values["scale"]),
                                         offset_s=None if values["auto_offset"] else float(values["offset_s"]))
                settings.validate()
                values.update({"start_a": start[0], "start_b": start[1], "end_a": end[0], "end_b": end[1]})
                sides, _ = recordingChannelSides(emg, headers, loadMappings(ROOT / "sensor_mapping.json"), record[1].get("channels", {}))
                result = analyze_angle_emg(emg, pedal, sides, settings)
            tables = {key: Markup(result[key].to_html(index=False, classes="table table-sm data-table", border=0,
                                                      na_rep="—", escape=True)) for key in ("summary", "profiles", "cycles")}
            result["settings_sync_json"] = json.dumps({"settings": result["settings"], "sync": result["sync"]}, ensure_ascii=False, indent=2, default=str)
            result["profile_charts"] = angle_profile_charts(result)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            error = str(exc)
    else:
        error = "這筆測試缺少原始 EMG 與踏板角度或曲柄事件 CSV，無法執行 Qt 原本的逐圈踩踏角度分析。"
    return render_template("angle.html", record=record[0], available=kinds, result=result, tables=tables,
                           settings_values=values, source_ready=source_ready, analysis_error=error,
                           database_result=database_result,
                           can_analyze=source_ready or database_result is not None)




__all__ = ["analysis_bp"]
