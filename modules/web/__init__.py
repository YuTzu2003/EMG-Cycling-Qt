"""Flask application factory and Blueprint registration for the web portal."""
from __future__ import annotations

import os
import secrets
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from flask import Flask, abort, render_template, request, session
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import auth_bp
from .portal import portal_bp, window_comparison

ROOT = Path(__file__).resolve().parents[2]


def create_app(test_config: dict | None = None) -> Flask:
    """Create the Flask application and register its web Blueprints."""
    app = Flask(__name__, template_folder="templates", static_folder=str(ROOT / "static"))
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

    app.extensions["login_failures"] = OrderedDict()
    app.extensions["login_lock"] = Lock()

    @app.before_request
    def require_https():
        if app.config["REQUIRE_HTTPS"] and not request.is_secure:
            abort(403, "HTTPS proxy is required for this site.")

    @app.context_processor
    def security_context():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return {"csrf_token": session["csrf"]}

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return render_template("error.html", message="上傳檔案超過 128 MB 限制。"), 413

    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    return app
