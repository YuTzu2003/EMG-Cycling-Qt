"""Patient registration and testing-record routes."""
from __future__ import annotations

import math
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for

from modules.blueprints.analysis.routes import _list_sessions as _list_analysis_sessions
from modules.services.database import connect as _connection




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
