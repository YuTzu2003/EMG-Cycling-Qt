"""Patient registration and testing-record routes."""
from __future__ import annotations

import math
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from modules.blueprints.analysis.routes import _list_sessions as _list_analysis_sessions
from modules.services.database import connect as _connection
from modules.services.patient_deletion import delete_patient as _delete_patient
from modules.blueprints.analysis.routes import _clear_session_cache




patient_bp = Blueprint("patients", __name__, template_folder="templates")


def _rows(cursor, values): return [dict(zip([column[0] for column in cursor.description], row)) for row in values]


def _duration_label(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "未記錄"
    if not math.isfinite(seconds) or seconds < 0:
        return "未記錄"
    minutes, remaining = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小時 {minutes:02d} 分 {remaining:02d} 秒"
    if minutes:
        return f"{minutes} 分 {remaining:02d} 秒"
    return f"{remaining} 秒"

def _list_patients():
    with _connection() as db:
        cursor=db.cursor(); cursor.execute("SELECT PatientID AS patient_id, Name AS name, Sex AS gender, Notes AS notes FROM dbo.Patients ORDER BY Name, PatientID"); return _rows(cursor, cursor.fetchall())

def _save_patient(patient_id, name, gender="未指定", notes=""):
    patient_id, name=patient_id.strip(), name.strip()
    if not patient_id or not name: raise ValueError("受測者編號與姓名不可空白。")
    with _connection() as db:
        cursor=db.cursor(); cursor.execute("UPDATE dbo.Patients SET Name=%s, Sex=%s, Notes=%s WHERE PatientID=%s", (name, gender, notes.strip()[:1000], patient_id))
        if cursor.rowcount == 0: cursor.execute("INSERT INTO dbo.Patients (PatientID, Name, Sex, Notes) VALUES (%s,%s,%s,%s)", (patient_id, name, gender, notes.strip()[:1000]))
        db.commit()

@patient_bp.route("/patients", methods=["GET", "POST"])
def patients():
    if request.method == "POST":
        try:
            _save_patient(
                request.form.get("patient_id", ""),
                request.form.get("name", ""),
                request.form.get("gender", "未指定"),
                request.form.get("notes", ""),
            )
        except ValueError as exc:
            flash(str(exc), "danger")
        else:
            flash("受測者資料已儲存。", "success")
            return redirect(url_for("patients.patients"))
    return render_template("patients.html", patients=_list_patients())


def _update_patient(patient_id, name, gender, notes):
    name, notes = name.strip(), notes.strip()
    if not name or len(name) > 100:
        raise ValueError("姓名不可空白，且最多 100 字。")
    if gender not in {"未指定", "男性", "女性", "其他"}:
        raise ValueError("請選擇有效的性別。")
    if len(notes) > 1000:
        raise ValueError("備註最多 1000 字。")
    with _connection() as db:
        cursor = db.cursor()
        try:
            cursor.execute("UPDATE dbo.Patients SET Name=%s, Sex=%s, Notes=%s WHERE PatientID=%s",
                           (name, gender, notes, patient_id))
            if cursor.rowcount == 0:
                raise LookupError("此受測者已不存在，請重新整理清單。")
            db.commit()
        except Exception:
            db.rollback()
            raise


@patient_bp.post("/patients/<patient_id>/edit")
def edit_patient(patient_id: str):
    try:
        _update_patient(patient_id, request.form.get("name", ""), request.form.get("gender", "未指定"),
                        request.form.get("notes", ""))
    except (ValueError, LookupError) as exc:
        flash(str(exc), "warning")
    except Exception:
        current_app.logger.exception("Patient update failed")
        flash("受測者資料修改失敗，請稍後重試。", "danger")
    else:
        flash("受測者資料已更新。Qt 請按「重新載入受測者」取得最新資料。", "success")
    return redirect(url_for("patients.patients"))


@patient_bp.post("/patients/<patient_id>/delete")
def delete_patient(patient_id: str):
    if request.form.get("confirm_patient_id", "").strip() != patient_id:
        flash("刪除未執行：請輸入正確的受測者編號。", "warning")
        return redirect(url_for("patients.patients"))
    try:
        result = _delete_patient(patient_id)
    except Exception:
        current_app.logger.exception("Patient deletion failed")
        flash("刪除失敗，請檢查資料庫連線或本地檔案權限後再重試。", "danger")
        return redirect(url_for("patients.patients"))
    if result is None:
        abort(404)
    # These in-memory caches can otherwise keep serving a deleted test.
    if result.test_ids:
        _clear_session_cache(result.test_ids[0])
    if result.warnings:
        for warning in result.warnings:
            current_app.logger.warning("Patient deletion: %s", warning)
        flash("受測者及資料庫內所有測試已刪除，但仍有本地檔案未清除，請檢查伺服器紀錄。網站無法清除 Qt 另外輸出的副本。", "warning")
    else:
        flash(f"受測者、{len(result.test_ids)} 筆測試與網站管理的本地原始檔、分析快取已永久刪除。", "success")
    return redirect(url_for("patients.patients"))


@patient_bp.get("/sessions")
def sessions():
    patient_id = request.args.get("patient_id", "").strip()
    search_query = request.args.get("q", "").strip()[:100]
    source_filter = request.args.get("source", "")
    if source_filter not in {"", "qt", "csv"}:
        source_filter = ""
    records = _list_analysis_sessions(patient_id or None)
    patient_record_count = len(records)
    if source_filter:
        records = [row for row in records if ("qt" if row["source"] == "qt" else "csv") == source_filter]
    if search_query:
        needle = search_query.casefold()
        records = [row for row in records if any(needle in str(row.get(field) or "").casefold()
                                                 for field in ("name", "patient_id", "session_id", "started_at"))]
    for row in records:
        row["duration_label"] = _duration_label(row.get("duration_seconds"))
    return render_template(
        "sessions.html",
        patients=_list_patients(),
        selected_patient=patient_id,
        search_query=search_query,
        source_filter=source_filter,
        patient_record_count=patient_record_count,
        sessions=records,
    )




__all__ = ["patient_bp"]
