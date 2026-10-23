"""Flask application factory and feature Blueprint registration."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Blueprint, Flask, abort, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from .analysis.routes import _list_sessions, analysis_bp
from .patient import patient_bp
from .patient.routes import _list_patients

ROOT = Path(__file__).resolve().parents[2]
general_bp = Blueprint("general", __name__)


@general_bp.get("/")
def dashboard():
    """Render the common home page with current patients and recordings."""
    return render_template(
        "dashboard.html",
        patients=_list_patients(),
        sessions=_list_sessions()[:8],
    )


def create_app(test_config: dict | None = None) -> Flask:
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

    @app.before_request
    def require_https():
        if app.config["REQUIRE_HTTPS"] and not request.is_secure:
            abort(403, "HTTPS proxy is required for this site.")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return render_template("error.html", message="上傳檔案不可超過 128 MB。"), 413

    app.register_blueprint(general_bp)
    app.register_blueprint(patient_bp)
    app.register_blueprint(analysis_bp)
    return app


__all__ = ["create_app"]
