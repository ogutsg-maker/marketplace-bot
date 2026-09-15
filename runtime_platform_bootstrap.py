"""Runtime compatibility/bootstrap layer for Armenia AI Guide."""
from __future__ import annotations

import importlib
import logging
from functools import wraps

import aiohttp
from aiohttp import web

_original_application_init = web.Application.__init__


def _document_download_url(pid: int, doc_id: int) -> str:
    return f"/api/admin/partner-applications/{pid}/documents/{doc_id}/proxy"


async def _admin_document_proxy(request: web.Request):
    from stage3_partner_verification import _admin_telegram_id, _db_fetchone, _storage_signed_url

    _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone(
        "SELECT original_filename,mime_type,storage_path,file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s",
        (doc_id, pid),
    )
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)

    filename = str(row.get("original_filename") or "document").replace('"', "")
    mime = row.get("mime_type") or "application/octet-stream"
    if row.get("file_data") is not None:
        return web.Response(body=bytes(row["file_data"]), content_type=mime,
                            headers={"Content-Disposition": f'inline; filename="{filename}"'})
    if not row.get("storage_path"):
        return web.json_response({"ok": False, "error": "document_file_not_available"}, status=404)

    try:
        signed = await _storage_signed_url(row["storage_path"], 300)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(signed) as response:
                if response.status != 200:
                    return web.json_response({"ok": False, "error": "storage_download_failed"}, status=502)
                data = await response.read()
        return web.Response(body=data, content_type=mime,
                            headers={"Content-Disposition": f'inline; filename="{filename}"'})
    except Exception as exc:
        logging.getLogger(__name__).exception("Verification document proxy failed: %s", doc_id)
        return web.json_response({"ok": False, "error": "document_open_failed", "details": str(exc)[:500]}, status=502)


async def _legacy_document_open(request: web.Request):
    """Compatibility response used by the existing admin.html openDoc()."""
    from stage3_partner_verification import _admin_telegram_id, _db_fetchone

    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone(
        "SELECT id,partner_id,storage_path,file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s",
        (doc_id, pid),
    )
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)
    return web.json_response({
        "ok": True,
        "admin_id": admin_id,
        "url": _document_download_url(pid, doc_id),
        "source": "proxy",
    })


def _bootstrap(app: web.Application) -> None:
    main = importlib.import_module("__main__")
    db = getattr(main, "db", None)
    ai = getattr(main, "ai", None)
    bot = getattr(main, "bot", None)
    if db is None or ai is None:
        return

    from platform_schema import ensure_platform_schema
    ensure_platform_schema()

    if not getattr(db, "_armenia_direction_bridge", False):
        original_set = db.set_master_categories

        @wraps(original_set)
        def bridged_set_master_categories(user_id, category_ids):
            result = original_set(user_id, category_ids)
            try:
                from partner_directions_api import ensure_initial_partner_direction
                partner = db.get_partner_by_user(user_id)
                if partner:
                    ensure_initial_partner_direction(partner["id"], user_id)
            except Exception:
                logging.getLogger(__name__).exception("Partner direction bridge failed for %s", user_id)
            return result

        db.set_master_categories = bridged_set_master_categories
        db._armenia_direction_bridge = True

    # Existing main.py registers this function on the legacy /open route. Replace
    # it before Application construction so Storage documents are opened through
    # our authenticated proxy instead of exposing a signed URL to Telegram WebView.
    main.api_admin_partner_document_open = _legacy_document_open

    from partner_directions_api import register_partner_direction_routes
    register_partner_direction_routes(app, db=db, bot=bot)
    from client_api import register_client_routes
    register_client_routes(app, ai)
    from admin_ai_api import register_admin_ai_routes
    register_admin_ai_routes(app, ai, bot=bot)

    if not getattr(app, "_armenia_document_proxy_registered", False):
        app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/proxy", _admin_document_proxy)
        app._armenia_document_proxy_registered = True

    app["platform_bootstrap_ready"] = True


def _application_init(self, *args, **kwargs):
    _original_application_init(self, *args, **kwargs)
    _bootstrap(self)


if not getattr(web.Application, "_armenia_platform_bootstrapped", False):
    web.Application.__init__ = _application_init
    web.Application._armenia_platform_bootstrapped = True
