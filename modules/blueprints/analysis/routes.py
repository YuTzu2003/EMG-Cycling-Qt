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
from modules.blueprints.analysis.pedal.analysis import analyze_pedal_records
from modules.blueprints.analysis.emg.reporting import render_report_html
from modules.blueprints.analysis.emg.session_analysis import add_point, analyze, new_session, summary_lines
from modules.blueprints.analysis.emg.session_compare import compare_sessions, comparison_charts
from modules.blueprints.analysis.emg.visuals import angle_profile_charts


ROOT = Path(__file__).resolve().parents[3]
analysis_bp = Blueprint("analysis", __name__, template_folder="templates")

# SQL operations belong to this analysis Blueprint.  Deployment schema is managed separately.

def _rows(cursor, values):
    return [dict(zip([column[0] for column in cursor.description], row)) for row in values]

def _list_patients():
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT PatientID AS patient_id, Name AS name, Sex AS gender, Notes AS notes FROM dbo.Patients ORDER BY Name, PatientID")
        return _rows(cursor, cursor.fetchall())

def _get_patient(patient_id):
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT PatientID AS patient_id, Name AS name, Sex AS gender, Notes AS notes FROM dbo.Patients WHERE PatientID=?", (patient_id,))
        row = cursor.fetchone(); return _rows(cursor, [row])[0] if row else None

def _list_sessions(patient_id=None):
    with _connection() as db:
        cursor = db.cursor(); query = "SELECT a.TestID AS session_id, a.PatientID AS patient_id, p.Name AS name, a.StartedAt AS started_at, a.Source AS source, a.Status AS status, (SELECT COUNT(*) FROM dbo.RawFiles f WHERE f.TestID=a.TestID) AS file_count FROM dbo.SessionArchives a JOIN dbo.Patients p ON p.PatientID=a.PatientID"
        cursor.execute(query + (" WHERE a.PatientID=?" if patient_id else "") + " ORDER BY a.StartedAt DESC", (patient_id,) if patient_id else ())
        return _rows(cursor, cursor.fetchall())

def _save_recording(session, files, *, source="upload"):
    import json
    from datetime import datetime, timezone
    patient_id = str((session.get("subject") or {}).get("patientId") or "").strip()
    identifier = f"TEST-{uuid4()}"
    if not patient_id: raise ValueError("測試紀錄缺少病人 ID。")
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT 1 FROM dbo.Patients WHERE PatientID=?", (patient_id,))
        if not cursor.fetchone(): raise ValueError("找不到病人資料。")
        cursor.execute("INSERT INTO dbo.SessionArchives (TestID, PatientID, StartedAt, Source, Status, SessionJSON) VALUES (?,?,?,?,?,?)", (identifier, patient_id, str(session.get("started_at") or datetime.now(timezone.utc).isoformat()), source, "完成", json.dumps(session, ensure_ascii=False, allow_nan=False)))
        for kind, value in files.items():
            filename, content = value
            cursor.execute("INSERT INTO dbo.RawFiles (TestID, Kind, FileName, Content) VALUES (?,?,?,?)", (identifier, kind, filename, bytearray(content)))
        db.commit()
    return identifier

def _get_session(session_id):
    with _connection() as db:
        import json
        cursor = db.cursor(); cursor.execute("SELECT TestID AS session_id, PatientID AS patient_id, StartedAt AS started_at, Source AS source, Status AS status, SessionJSON AS session_json FROM dbo.SessionArchives WHERE TestID=?", (session_id,))
        row = cursor.fetchone()
        if not row: return None
        record = _rows(cursor, [row])[0]; return record, json.loads(record.pop("session_json"))

def _list_files(session_id):
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT Kind AS kind, FileName AS filename, DATALENGTH(Content) AS size FROM dbo.RawFiles WHERE TestID=? ORDER BY Kind", (session_id,)); return _rows(cursor, cursor.fetchall())

def _get_file(session_id, kind):
    with _connection() as db:
        cursor = db.cursor(); cursor.execute("SELECT FileName, Content FROM dbo.RawFiles WHERE TestID=? AND Kind=?", (session_id, kind)); row=cursor.fetchone(); return (str(row[0]), bytes(row[1])) if row else None


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
    result = analyze(document)
    pedal_performance = None
    performance_lines: list[str] = []
    if document.get("pedal"):
        try:
            # FTP is only needed for training-zone calculations. It remains
            # explicit until a per-patient FTP field is available.
            pedal_performance = analyze_pedal_records(document["pedal"], ftp_w=200)
            performance_lines = _pedal_performance_lines(pedal_performance)
        except (ValueError, KeyError, TypeError):
            # Old captures may not retain every timestamp field required by this optional analysis.
            performance_lines = ["此筆踏板原始資料缺少進階效能分析所需欄位。"]
    try:
        report = render_report_html(document)
        report_body = report.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    except ValueError as exc:
        flash(f"完整報告無法產生：{exc}", "warning")
        report_body = ""
    files = _list_files(session_id)
    source_kinds = {item["kind"] for item in files}
    return render_template("session_detail.html", record=record, files=files, analysis=result,
                           lines=summary_lines(result), report_body=Markup(report_body),
                           analysis_sections=_analysis_sections(document, result, pedal_performance, source_kinds),
                           pedal_performance=pedal_performance, performance_lines=performance_lines,
                           emg_chart_series=_emg_chart_series(document))


@analysis_bp.get("/sessions/<session_id>/report")
def report(session_id: str):
    found = _get_session(session_id)
    if found is None:
        abort(404)
    try:
        return render_report_html(found[1]), 200, {"Content-Type": "text/html; charset=utf-8"}
    except ValueError as exc:
        return render_template("error.html", message=str(exc)), 422


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
    if request.method == "POST":
        if not ({"emg", "pedal"} <= kinds or {"emg", "angle"} <= kinds):
            flash("角度分析需要 EMG 與踏板或角度 CSV。", "danger")
        else:
            try:
                settings = AngleSettings(start=(float(request.form["start_a"]), float(request.form["start_b"])),
                                         end=(float(request.form["end_a"]), float(request.form["end_b"])))
                with temporary_files() as temp_path:
                    emg, pedal = temp_path("emg.csv"), temp_path("pedal.csv")
                    emg.write_bytes(_get_file(session_id, "emg")[1])
                    pedal.write_bytes(_get_file(session_id, "angle" if "angle" in kinds else "pedal")[1])
                    with emg.open(encoding="utf-8-sig", newline="") as stream:
                        headers = next(csv.reader(stream), [])
                    sides, _ = recordingChannelSides(emg, headers, loadMappings(ROOT / "sensor_mapping.json"), record[1].get("channels", {}))
                    result = analyze_angle_emg(emg, pedal, sides, settings)
                tables = {key: Markup(result[key].to_html(index=False, classes="table table-sm data-table", border=0,
                                                          na_rep="—", escape=True)) for key in ("summary", "profiles", "cycles")}
                result["settings_sync_json"] = json.dumps({"settings": result["settings"], "sync": result["sync"]}, ensure_ascii=False, indent=2, default=str)
                result["profile_charts"] = angle_profile_charts(result)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                flash(f"角度分析失敗：{exc}", "danger")
    return render_template("angle.html", record=record[0], available=kinds, result=result, tables=tables)




__all__ = ["analysis_bp"]
