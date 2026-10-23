"""Shared request security helpers for the web blueprints."""
from __future__ import annotations

import hmac
import os
from functools import wraps

from flask import abort, current_app, redirect, request, session, url_for


def login_required(view):
    """Require the local portal password before serving a protected view."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        password = current_app.config.get("WEB_PASSWORD", os.environ.get("WEB_PASSWORD", ""))
        if not password:
            abort(503, "尚未設定 WEB_PASSWORD，無法啟動網站登入。")
        if not session.get("logged_in"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def require_csrf() -> None:
    """Reject state-changing requests without this browser session's token."""
    token = session.get("csrf")
    if not token or not hmac.compare_digest(token, request.form.get("csrf", "")):
        abort(400, "CSRF 驗證失敗，請重新載入頁面後再試一次。")
