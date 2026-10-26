"""Flask application factory and feature Blueprint registration."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from flask import Blueprint, Flask, abort, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from .analysis.routes import _list_sessions, analysis_bp
from .patient import patient_bp
from .patient.routes import _duration_label, _list_patients

ROOT = Path(__file__).resolve().parents[2]
general_bp = Blueprint("general", __name__)


@general_bp.get("/")
def dashboard():
    """Render the common home page with current patients and recordings."""
    sessions = _list_sessions()
    for row in sessions[:5]:
        row["duration_label"] = _duration_label(row.get("duration_seconds"))
        test_item = str(row.get("test_item") or "").strip()
        if not test_item or test_item.casefold() in {"csv import", "csv_import", "csv 匯入", "csv匯入"}:
            items = [label for key, label in (("emg_count", "肌電 RMS 測試"), ("pedal_count", "踏板測試"),
                                             ("trainer_count", "訓練器測試"))
                     if row.get(key, 0)]
            if not items and row.get("baseline_count", 0):
                items.append("肌電基準測試")
            row["test_item"] = "、".join(items) or "未記錄"
    return render_template(
        "dashboard.html",
        patients=_list_patients(),
        sessions=sessions[:5],
        session_count=len(sessions),
    )


def create_app(test_config: dict | None = None) -> Flask:
    load_dotenv(ROOT / ".env", override=False)
    from modules.services.file_storage import cleanup_temporary_files
    cleanup_temporary_files()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config.update(
        SECRET_KEY=os.environ.get("WEB_SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=128 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        REQUIRE_HTTPS=os.environ.get("WEB_BEHIND_HTTPS_PROXY") == "1",
    )
    if test_config:
        app.config.update(test_config)
    app.config["SESSION_COOKIE_SECURE"] = bool(app.config["REQUIRE_HTTPS"])
    if app.config["REQUIRE_HTTPS"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    @app.context_processor
    def csrf_context():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return {"csrf_token": token}

    @app.before_request
    def protect_requests():
        if app.config["REQUIRE_HTTPS"] and not request.is_secure:
            abort(403, "HTTPS proxy is required for this site.")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                abort(400, "Invalid CSRF token.")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return render_template("error.html", message="上傳檔案不可超過 128 MB。"), 413

    app.register_blueprint(general_bp)
    app.register_blueprint(patient_bp)
    app.register_blueprint(analysis_bp)
    return app


__all__ = ["create_app"]
