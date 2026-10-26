"""Render the existing web report to PDF without a client-side print dialog."""
from __future__ import annotations

import json
import os
from threading import BoundedSemaphore

from flask import Flask

_render_slots = BoundedSemaphore(2)
_ORIGIN = "https://report.internal"


def render_pdf(app: Flask, report_path: str, *, session_cookie: str | None = None, phase: int = 0) -> bytes:
    """Serve local report requests through Flask, independent of public host/port.

    Only the fixed synthetic origin is intercepted. A separate client carries
    the signed user session, while external chart/font resources load normally.
    No public URL from request headers is used as a browser navigation target.
    """
    if not _render_slots.acquire(timeout=1):
        raise RuntimeError("目前有其他 PDF 正在產生，請稍後重試。")
    try:
        from playwright.sync_api import Error, sync_playwright

        client = app.test_client()
        if session_cookie:
            client.set_cookie(app.config["SESSION_COOKIE_NAME"], session_cookie, domain="report.internal", secure=True)

        def serve_local(route):
            from urllib.parse import urlsplit
            url = urlsplit(route.request.url)
            response = client.get(url.path + ("?" + url.query if url.query else ""), base_url=_ORIGIN)
            route.fulfill(status=response.status_code, body=response.get_data(),
                          headers={"content-type": response.content_type, "cache-control": "no-store"})

        with sync_playwright() as playwright:
            channel = os.getenv("PDF_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True)
            except Error as exc:
                raise RuntimeError("PDF 瀏覽器無法啟動。請確認伺服器已安裝 Microsoft Edge，或設定 PDF_BROWSER_CHANNEL。") from exc
            try:
                context = browser.new_context(viewport={"width": 1100, "height": 900}, service_workers="block")
                context.route(_ORIGIN + "/**", serve_local)
                context.add_init_script("sessionStorage.setItem('angle-phase:' + location.pathname, " + json.dumps(str(phase)) + ");")
                page = context.new_page()
                page.goto(_ORIGIN + report_path, timeout=180_000, wait_until="load")
                page.wait_for_function("""() => document.getElementById('report-print')?.disabled === false
                    || document.getElementById('report-progress')?.classList.contains('text-danger')""", timeout=360_000)
                if page.locator("#report-print").is_disabled():
                    raise RuntimeError(page.locator("#report-progress").inner_text())
                return page.pdf(format="A4", landscape=True, prefer_css_page_size=True,
                                print_background=True, display_header_footer=False)
            except Error as exc:
                raise RuntimeError("PDF 產生失敗或逾時，請減少匯出項目或稍後重試。") from exc
            finally:
                browser.close()
    except ImportError as exc:
        raise RuntimeError("PDF 套件尚未安裝，請在伺服器執行 uv sync。") from exc
    except OSError as exc:
        raise RuntimeError("PDF 背景瀏覽器無法執行，請檢查伺服器的程序與暫存資料夾權限。") from exc
    finally:
        _render_slots.release()
