"""Authentication blueprint for the local analysis portal."""
from __future__ import annotations

import hmac
import os
import secrets
import time

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from .security import login_required, require_csrf

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        require_csrf()
        source = request.remote_addr or "unknown"
        now = time.monotonic()
        failures = current_app.extensions["login_failures"]
        lock = current_app.extensions["login_lock"]
        with lock:
            recent = [attempt for attempt in failures.get(source, []) if now - attempt < LOGIN_WINDOW_SECONDS]
            if recent:
                failures[source] = recent
                failures.move_to_end(source)
            else:
                failures.pop(source, None)
            if len(recent) >= LOGIN_MAX_FAILURES:
                abort(429, "登入嘗試次數過多，請稍後再試。")
        password = current_app.config.get("WEB_PASSWORD", os.environ.get("WEB_PASSWORD", ""))
        if password and hmac.compare_digest(request.form.get("password", ""), password):
            with lock:
                failures.pop(source, None)
            session.clear()
            session["logged_in"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("portal.dashboard"))
        with lock:
            failures[source] = recent + [now]
            failures.move_to_end(source)
            if len(failures) > 4096:
                failures.popitem(last=False)
        flash("密碼不正確。", "danger")
    return render_template("login.html")


@auth_bp.post("/logout")
@login_required
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("auth.login"))
