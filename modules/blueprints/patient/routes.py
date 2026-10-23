"""Patient registration and testing-record routes."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for

from modules.blueprints.analysis.routes import _list_sessions as _list_analysis_sessions
from modules.services.database import connect as _connection




patient_bp = Blueprint("patients", __name__, template_folder="templates")


def _rows(cursor, values): return [dict(zip([column[0] for column in cursor.description], row)) for row in values]

def _list_patients():
    with _connection() as db:
        cursor=db.cursor(); cursor.execute("SELECT PatientID AS patient_id, Name AS name, Sex AS gender, Notes AS notes FROM dbo.Patients ORDER BY Name, PatientID"); return _rows(cursor, cursor.fetchall())

def _save_patient(patient_id, name, gender="未指定", notes=""):
    patient_id, name=patient_id.strip(), name.strip()
    if not patient_id or not name: raise ValueError("病人 ID 與姓名不可空白。")
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
            flash("病人資料已儲存。", "success")
            return redirect(url_for("patients.patients"))
    return render_template("patients.html", patients=_list_patients())


@patient_bp.get("/sessions")
def sessions():
    patient_id = request.args.get("patient_id", "")
    return render_template(
        "sessions.html",
        patients=_list_patients(),
        selected_patient=patient_id,
        sessions=_list_analysis_sessions(patient_id or None),
    )




__all__ = ["patient_bp"]
