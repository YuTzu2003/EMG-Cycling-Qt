"""Patient, upload, and analysis routes for the EMG web portal."""
from __future__ import annotations

import csv
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from markupsafe import Markup
from werkzeug.utils import secure_filename

from modules import archive
from modules.emg.sensor_mapping import loadMappings, recordingChannelSides
from modules.importers import attach_pedal_csv, import_emg_csv
from modules.pedal.angle_emg import AngleSettings, analyze_angle_emg
from modules.pedal.analysis import analyze_pedal_records
from modules.reporting import render_report_html
from modules.session_analysis import add_point, analyze, new_session, summary_lines
from modules.session_compare import compare_sessions, comparison_charts
from modules.visuals import angle_profile_charts

from .security import login_required, require_csrf

ROOT = Path(__file__).resolve().parents[2]
portal_bp = Blueprint("portal", __name__)


def temporary_root() -> Path:
    path = Path(os.environ.get("TEMP_DIR", ROOT / "data" / "tmp"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def temporary_files():
    created: list[Path] = []

    def new_path(name: str) -> Path:
        path = temporary_root() / f"{uuid4()}_{name}"
        created.append(path)
        return path

    try:
        yield new_path
    finally:
        for path in created:
            path.unlink(missing_ok=True)


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


@portal_bp.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html", patients=archive.list_patients(), sessions=archive.list_sessions()[:8])


@portal_bp.route("/patients", methods=["GET", "POST"])
@login_required
def patients():
    if request.method == "POST":
        require_csrf()
        try:
            archive.save_patient(request.form.get("patient_id", ""), request.form.get("name", ""),
                                 request.form.get("gender", "未填寫"), request.form.get("notes", ""))
        except ValueError as exc:
            flash(str(exc), "danger")
        else:
            flash("病人資料已儲存，Qt 收集程式重新整理後即可選取。", "success")
            return redirect(url_for("portal.patients"))
    return render_template("patients.html", patients=archive.list_patients())


@portal_bp.get("/sessions")
@login_required
def sessions():
    patient_id = request.args.get("patient_id", "")
    return render_template("sessions.html", patients=archive.list_patients(), selected_patient=patient_id,
                           sessions=archive.list_sessions(patient_id or None))


@portal_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        require_csrf()
        patient = archive.get_patient(request.form.get("patient_id", ""))
        if patient is None:
            flash("請先選擇已建立的病人。", "danger")
            return redirect(url_for("portal.upload"))
        uploads = {kind: request.files.get(kind) for kind in ("emg", "pedal", "trainer", "angle")}
        uploads = {kind: file for kind, file in uploads.items() if file and file.filename}
        if not uploads or any(Path(file.filename).suffix.lower() != ".csv" for file in uploads.values()):
            flash("請至少上傳一個 CSV 檔案。", "danger")
            return redirect(url_for("portal.upload"))
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
                identifier = archive.save_recording(recording, files, source="upload")
            flash("CSV 已匯入並建立分析測試。", "success")
            return redirect(url_for("portal.session_detail", session_id=identifier))
        except (ValueError, OSError, TypeError, OverflowError) as exc:
            flash(f"匯入失敗：{exc}", "danger")
    return render_template("upload.html", patients=archive.list_patients())


@portal_bp.get("/sessions/<session_id>")
@login_required
def session_detail(session_id: str):
    found = archive.get_session(session_id)
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
    files = archive.list_files(session_id)
    source_kinds = {item["kind"] for item in files}
    return render_template("session_detail.html", record=record, files=files, analysis=result,
                           lines=summary_lines(result), report_body=Markup(report_body),
                           analysis_sections=_analysis_sections(document, result, pedal_performance, source_kinds),
                           pedal_performance=pedal_performance, performance_lines=performance_lines)


@portal_bp.get("/sessions/<session_id>/report")
@login_required
def report(session_id: str):
    found = archive.get_session(session_id)
    if found is None:
        abort(404)
    try:
        return render_report_html(found[1]), 200, {"Content-Type": "text/html; charset=utf-8"}
    except ValueError as exc:
        return render_template("error.html", message=str(exc)), 422


@portal_bp.get("/sessions/<session_id>/files/<kind>")
@login_required
def download_file(session_id: str, kind: str):
    found = archive.get_file(session_id, kind)
    if found is None:
        abort(404)
    return send_file(io.BytesIO(found[1]), mimetype="text/csv" if kind != "info" else "text/plain",
                     as_attachment=True, download_name=secure_filename(found[0]) or f"{kind}.csv")


@portal_bp.route("/compare", methods=["GET", "POST"])
@login_required
def compare():
    all_patients = archive.list_patients()
    patient_id = request.values.get("patient_id", "")
    choices = archive.list_sessions(patient_id) if patient_id else []
    result = None
    window_rows: list[dict] = []
    charts = None
    early_s, late_s = 30, 30
    if request.method == "POST":
        require_csrf()
        try:
            early_s, late_s = int(request.form.get("early_s", 30)), int(request.form.get("late_s", 30))
            if not (1 <= early_s <= 600 and 1 <= late_s <= 600):
                raise ValueError
        except ValueError:
            abort(400, "時間窗必須介於 1 到 600 秒。")
        first, second = archive.get_session(request.form.get("first", "")), archive.get_session(request.form.get("second", ""))
        if not first or not second or first[0]["patient_id"] != patient_id or second[0]["patient_id"] != patient_id:
            flash("請選擇同一位病人的兩筆不同測試。", "danger")
        elif first[0]["session_id"] == second[0]["session_id"]:
            flash("請選擇兩筆不同測試。", "danger")
        else:
            result = compare_sessions(first[1], second[1])
            window_rows = window_comparison(first[1], second[1], early_s, late_s)
            charts = Markup(comparison_charts(first[1], second[1], result))
    return render_template("compare.html", patients=all_patients, patient_id=patient_id, choices=choices, result=result,
                           window_rows=window_rows, charts=charts, early_s=early_s, late_s=late_s)


@portal_bp.route("/sessions/<session_id>/angle", methods=["GET", "POST"])
@login_required
def angle(session_id: str):
    record = archive.get_session(session_id)
    if record is None:
        abort(404)
    available = archive.list_files(session_id)
    kinds = {file["kind"] for file in available}
    result = tables = None
    if request.method == "POST":
        require_csrf()
        if not ({"emg", "pedal"} <= kinds or {"emg", "angle"} <= kinds):
            flash("角度分析需要 EMG 與踏板或角度 CSV。", "danger")
        else:
            try:
                settings = AngleSettings(start=(float(request.form["start_a"]), float(request.form["start_b"])),
                                         end=(float(request.form["end_a"]), float(request.form["end_b"])))
                with temporary_files() as temp_path:
                    emg, pedal = temp_path("emg.csv"), temp_path("pedal.csv")
                    emg.write_bytes(archive.get_file(session_id, "emg")[1])
                    pedal.write_bytes(archive.get_file(session_id, "angle" if "angle" in kinds else "pedal")[1])
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
