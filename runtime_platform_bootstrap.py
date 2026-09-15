"""Runtime bridge for the Armenia AI Guide platform APIs.

Loaded from config.py before main creates aiohttp.Application. The bridge keeps
legacy startup intact while registering the newer platform APIs and compatibility
routes that are required by the current Web Apps.
"""
from __future__ import annotations

import importlib
import logging
from functools import wraps

import aiohttp
from aiohttp import web

_original_application_init = web.Application.__init__


async def _admin_document_proxy(request: web.Request):
    """Return a verification document as bytes, including Supabase Storage files.

    The old admin page could open a signed Storage URL directly, which is fragile
    inside Telegram WebView. This endpoint authenticates the admin first and
    proxies either Storage or database-backed documents as an inline response.
    """
    from stage3_partner_verification import (
        _admin_telegram_id,
        _db_fetchone,
        _storage_signed_url,
    )

    _admin_telegram_id(
        request,
        request.app.get("stage3_bot_token"),
        request.app.get("stage3_admin_id"),
    )
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone(
        "SELECT original_filename,mime_type,storage_path,file_data "
        "FROM partner_verification_documents "
        "WHERE id=%s AND partner_id=%s",
        (doc_id, pid),
    )
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)

    filename = str(row.get("original_filename") or "document").replace('"', "")
    mime = row.get("mime_type") or "application/octet-stream"

    if row.get("file_data") is not None:
        return web.Response(
            body=bytes(row["file_data"]),
            content_type=mime,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    if not row.get("storage_path"):
        return web.json_response({"ok": False, "error": "document_file_not_available"}, status=404)

    try:
        signed = await _storage_signed_url(row["storage_path"], 300)
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(signed) as response:
                if response.status != 200:
                    body = await response.text()
                    raise RuntimeError(f"storage_download_failed:{response.status}:{body[:300]}")
                data = await response.read()
        return web.Response(
            body=data,
            content_type=mime,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("Verification document proxy failed: %s", doc_id)
        return web.json_response({"ok": False, "error": "document_open_failed", "details": str(exc)[:500]}, status=502)


def _bootstrap(app: web.Application) -> None:
    try:
        main = importlib.import_module("__main__")
        db = getattr(main, "db", None)
        ai = getattr(main, "ai", None)
        bot = getattr(main, "bot", None)
        if db is None or ai is None:
            return

        from platform_schema import ensure_platform_schema
        ensure_platform_schema()

        # Legacy onboarding writes master_skills. Mirror those selections into
        # the new partner_directions tables used by the admin/cabinet.
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
                    logging.getLogger(__name__).exception(
                        "Partner direction bridge failed for %s", user_id
                    )
                return result

            db.set_master_categories = bridged_set_master_categories
            db._armenia_direction_bridge = True

        from partner_directions_api import register_partner_direction_routes
        register_partner_direction_routes(app, db=db, bot=bot)

        from client_api import register_client_routes
        register_client_routes(app, ai)

        from admin_ai_api import register_admin_ai_routes
        register_admin_ai_routes(app, ai, bot=bot)

        # Admin document preview that works for both PostgreSQL fallback and
        # private Supabase Storage uploads.
        if not getattr(app, "_armenia_document_proxy_registered", False):
            app.router.add_get(
                "/api/admin/partner-applications/{id}/documents/{doc_id}/proxy",
                _admin_document_proxy,
            )
            app._armenia_document_proxy_registered = True

        app["platform_bootstrap_ready"] = True
    except Exception:
        logging.getLogger(__name__).exception("Complete platform bootstrap failed")
        raise


def _application_init(self, *args, **kwargs):
    _original_application_init(self, *args, **kwargs)
    _bootstrap(self)


if not getattr(web.Application, "_armenia_platform_bootstrapped", False):
    web.Application.__init__ = _application_init
    web.Application._armenia_platform_bootstrapped = True
