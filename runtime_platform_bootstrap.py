"""Runtime compatibility/bootstrap layer for Armenia AI Guide."""
from __future__ import annotations

import importlib
import logging
import os
from functools import wraps

import aiohttp
import psycopg
from aiohttp import web

_original_application_init = web.Application.__init__


def _document_download_url(pid: int, doc_id: int) -> str:
    return f"/api/admin/partner-applications/{pid}/documents/{doc_id}/proxy"


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _storage_config():
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.getenv("SUPABASE_KEY", "").strip()
    )
    bucket = (
        os.getenv("SUPABASE_STORAGE_BUCKET", "partner-verification-documents").strip()
        or "partner-verification-documents"
    )
    if not base or not key:
        raise RuntimeError("Supabase Storage configuration is missing")
    return base, key, bucket


def _db_fetchone(sql, params=()):
    # PgBouncer/Supabase transaction pooling: disable automatic prepared statements.
    with psycopg.connect(_database_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))


async def _storage_direct_download(path: str) -> bytes:
    """Download a private Storage object directly with the server-side key."""
    base, key, bucket = _storage_config()
    url = f"{base}/storage/v1/object/{bucket}/{path}"
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            data = await response.read()
            if response.status != 200:
                raise RuntimeError(
                    f"Supabase Storage download failed ({response.status}): {data[:500].decode(errors='replace')}"
                )
            return data


async def _storage_signed_download(path: str) -> bytes:
    """Download through a short-lived signed URL."""
    from stage3_partner_verification import _storage_signed_url

    signed = await _storage_signed_url(path, 300)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(signed) as response:
            data = await response.read()
            if response.status != 200:
                raise RuntimeError(f"Signed Storage download failed ({response.status})")
            return data


async def _admin_document_proxy(request: web.Request):
    """Authenticated same-origin document endpoint used by the admin WebApp.

    It deliberately returns the actual file bytes instead of exposing a private
    Supabase URL to the browser. Storage download has a signed-URL path and a
    direct service-role fallback, so old uploads remain readable even when the
    Storage signing endpoint is unavailable.
    """
    from stage3_partner_verification import _admin_telegram_id

    _admin_telegram_id(
        request,
        request.app.get("stage3_bot_token"),
        request.app.get("stage3_admin_id"),
    )

    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone(
        "SELECT original_filename,mime_type,storage_path,file_data "
        "FROM partner_verification_documents WHERE id=%s AND partner_id=%s",
        (doc_id, pid),
    )
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)

    filename = str(row.get("original_filename") or "document").replace('"', "")
    mime = str(row.get("mime_type") or "application/octet-stream")

    try:
        if row.get("file_data") is not None:
            data = bytes(row["file_data"])
        elif row.get("storage_path"):
            try:
                data = await _storage_signed_download(str(row["storage_path"]))
            except Exception as signed_exc:
                logging.getLogger(__name__).warning(
                    "Signed document download failed, using direct Storage download: doc=%s error=%s",
                    doc_id,
                    signed_exc,
                )
                data = await _storage_direct_download(str(row["storage_path"]))
        else:
            return web.json_response(
                {"ok": False, "error": "document_file_not_available"},
                status=404,
            )

        return web.Response(
            body=data,
            content_type=mime,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "private, no-store",
            },
        )
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Verification document proxy failed: partner=%s document=%s",
            pid,
            doc_id,
        )
        return web.json_response(
            {
                "ok": False,
                "error": "document_open_failed",
                "details": str(exc)[:500],
            },
            status=502,
        )


async def _legacy_document_open(request: web.Request):
    """Compatibility JSON endpoint used by older admin.html builds."""
    from stage3_partner_verification import _admin_telegram_id

    admin_id = _admin_telegram_id(
        request,
        request.app.get("stage3_bot_token"),
        request.app.get("stage3_admin_id"),
    )
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone(
        "SELECT id FROM partner_verification_documents "
        "WHERE id=%s AND partner_id=%s",
        (doc_id, pid),
    )
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)
    return web.json_response(
        {
            "ok": True,
            "admin_id": admin_id,
            "url": _document_download_url(pid, doc_id),
            "source": "database",
        }
    )


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
                logging.getLogger(__name__).exception(
                    "Partner direction bridge failed for %s", user_id
                )
            return result

        db.set_master_categories = bridged_set_master_categories
        db._armenia_direction_bridge = True

    # Compatibility handlers are assigned before main.py registers its routes.
    main.api_admin_partner_document_open = _legacy_document_open
    main.api_admin_partner_document_open_file = _admin_document_proxy

    from partner_directions_api import register_partner_direction_routes
    register_partner_direction_routes(app, db=db, bot=bot)

    from client_api import register_client_routes
    register_client_routes(app, ai)

    from admin_ai_api import register_admin_ai_routes
    register_admin_ai_routes(app, ai, bot=bot)

    from marketplace_flow_api import register_marketplace_flow_routes
    register_marketplace_flow_routes(app)

    if not getattr(app, "_armenia_document_proxy_registered", False):
        app.router.add_get(
            "/api/admin/partner-applications/{id}/documents/{doc_id}/proxy",
            _admin_document_proxy,
        )
        app._armenia_document_proxy_registered = True

    app["platform_bootstrap_ready"] = True


def _application_init(self, *args, **kwargs):
    _original_application_init(self, *args, **kwargs)
    _bootstrap(self)


if not getattr(web.Application, "_armenia_platform_bootstrapped", False):
    web.Application.__init__ = _application_init
    web.Application._armenia_platform_bootstrapped = True
