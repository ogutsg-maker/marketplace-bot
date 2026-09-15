"""Runtime compatibility/bootstrap layer for Armenia AI Guide."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import logging
import os
import time
from functools import wraps

import aiohttp
import psycopg
from aiohttp import web

_original_application_init = web.Application.__init__


def _document_download_url(pid: int, doc_id: int, token: str | None = None) -> str:
    url = f"/api/admin/partner-applications/{pid}/documents/{doc_id}/proxy"
    return f"{url}?access={token}" if token else url


def _database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _storage_config():
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip() or os.getenv("SUPABASE_KEY", "").strip()
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "partner-verification-documents").strip() or "partner-verification-documents"
    if not base or not key:
        raise RuntimeError("Supabase Storage configuration is missing")
    return base, key, bucket


def _db_fetchone(sql, params=()):
    with psycopg.connect(_database_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if not row:
                return None
            return dict(zip([d.name for d in cur.description], row))


def _document_access_secret() -> bytes:
    return (os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip() or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()).encode()


def _make_document_access_token(pid: int, doc_id: int, ttl: int = 900) -> str:
    payload = {"pid": int(pid), "doc": int(doc_id), "exp": int(time.time()) + int(ttl)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(_document_access_secret(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _verify_document_access_token(token: str, pid: int, doc_id: int) -> bool:
    try:
        body, supplied = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(_document_access_secret(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(supplied, expected):
            return False
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode())
        return int(payload["pid"]) == int(pid) and int(payload["doc"]) == int(doc_id) and int(payload["exp"]) >= int(time.time())
    except Exception:
        return False


async def _storage_direct_download(path: str) -> bytes:
    base, key, bucket = _storage_config()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.get(f"{base}/storage/v1/object/{bucket}/{path}", headers={"Authorization": f"Bearer {key}", "apikey": key}) as response:
            data = await response.read()
            if response.status != 200:
                raise RuntimeError(f"Supabase Storage download failed ({response.status}): {data[:500].decode(errors='replace')}")
            return data


async def _storage_signed_download(path: str) -> bytes:
    from stage3_partner_verification import _storage_signed_url
    signed = await _storage_signed_url(path, 300)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.get(signed) as response:
            data = await response.read()
            if response.status != 200:
                raise RuntimeError(f"Signed Storage download failed ({response.status})")
            return data


async def _get_document(pid: int, doc_id: int):
    return _db_fetchone("SELECT original_filename,mime_type,storage_path,file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s", (doc_id, pid))


async def _document_bytes(row):
    if row.get("file_data") is not None:
        return bytes(row["file_data"])
    if row.get("storage_path"):
        try:
            return await _storage_signed_download(str(row["storage_path"]))
        except Exception as exc:
            logging.getLogger(__name__).warning("Signed document download failed, using direct Storage: %s", exc)
            return await _storage_direct_download(str(row["storage_path"]))
    raise RuntimeError("document_file_not_available")


async def _authorize_document(request, pid: int, doc_id: int):
    from stage3_partner_verification import _admin_telegram_id
    if request.headers.get("X-Telegram-Init-Data", "").strip():
        _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
        return
    if not _verify_document_access_token(request.query.get("access", ""), pid, doc_id):
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"document_access_required"}', content_type="application/json")


async def _admin_document_proxy(request: web.Request):
    pid, doc_id = int(request.match_info["id"]), int(request.match_info["doc_id"])
    await _authorize_document(request, pid, doc_id)
    row = await _get_document(pid, doc_id)
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)
    try:
        data = await _document_bytes(row)
        filename = str(row.get("original_filename") or "document").replace('"', "")
        mime = str(row.get("mime_type") or "application/octet-stream")
        return web.Response(body=data, content_type=mime, headers={"Content-Disposition": f'inline; filename="{filename}"', "Content-Length": str(len(data)), "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    except Exception as exc:
        logging.getLogger(__name__).exception("Verification document proxy failed: partner=%s document=%s", pid, doc_id)
        return web.json_response({"ok": False, "error": "document_open_failed", "details": str(exc)[:500]}, status=502)


async def _admin_document_viewer(request: web.Request):
    """Mobile-safe viewer; authentication is carried by the short-lived access token."""
    pid, doc_id = int(request.match_info["id"]), int(request.match_info["doc_id"])
    token = request.query.get("access", "").strip()
    if not _verify_document_access_token(token, pid, doc_id):
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"document_access_required"}', content_type="application/json")
    row = await _get_document(pid, doc_id)
    if not row:
        return web.Response(text="Документ не найден", status=404, content_type="text/plain")
    proxy = _document_download_url(pid, doc_id, token)
    filename = str(row.get("original_filename") or "document").replace('"', "&quot;")
    mime = str(row.get("mime_type") or "application/octet-stream")
    title = "Документ партнёра"
    if mime == "application/pdf":
        body = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>html,body{{margin:0;height:100%;background:#111827;color:#fff;font:16px system-ui}}header{{height:52px;display:flex;align-items:center;padding:0 12px;box-sizing:border-box;gap:10px;overflow:hidden}}header b{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}iframe{{display:block;width:100%;height:calc(100% - 52px);border:0;background:#fff}}a{{color:#fff;background:#2563eb;padding:8px 12px;border-radius:8px;text-decoration:none;white-space:nowrap}}</style><header><b>{filename}</b><a href="{proxy}" download>Скачать</a></header><iframe src="{proxy}"></iframe>'''
    elif mime.startswith("image/"):
        body = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>html,body{{margin:0;min-height:100%;background:#111827;color:#fff;font:16px system-ui}}header{{padding:12px;display:flex;justify-content:space-between;gap:10px}}main{{display:flex;justify-content:center;align-items:center;padding:10px;min-height:calc(100vh - 70px);box-sizing:border-box}}img{{max-width:100%;max-height:calc(100vh - 90px);object-fit:contain}}a{{color:#fff;background:#2563eb;padding:8px 12px;border-radius:8px;text-decoration:none;white-space:nowrap}}</style><header><b>{filename}</b><a href="{proxy}" download>Скачать</a></header><main><img src="{proxy}" alt="Документ"></main>'''
    else:
        body = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><p style="font:16px system-ui;padding:20px">{filename}</p><p style="padding:20px"><a href="{proxy}" download>Скачать документ</a></p>'''
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "private, no-store"})


async def _legacy_document_open(request: web.Request):
    from stage3_partner_verification import _admin_telegram_id
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid, doc_id = int(request.match_info["id"]), int(request.match_info["doc_id"])
    if not _db_fetchone("SELECT id FROM partner_verification_documents WHERE id=%s AND partner_id=%s", (doc_id, pid)):
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)
    token = _make_document_access_token(pid, doc_id)
    viewer = f"/api/admin/partner-applications/{pid}/documents/{doc_id}/viewer?access={token}"
    return web.json_response({"ok": True, "admin_id": admin_id, "url": viewer, "viewer_url": viewer, "source": "database"})


def _bootstrap(app: web.Application) -> None:
    main = importlib.import_module("__main__")
    db, ai, bot = getattr(main, "db", None), getattr(main, "ai", None), getattr(main, "bot", None)
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
        app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/proxy", _admin_document_proxy)
        app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/viewer", _admin_document_viewer)
        app._armenia_document_proxy_registered = True
    app["platform_bootstrap_ready"] = True


def _application_init(self, *args, **kwargs):
    _original_application_init(self, *args, **kwargs)
    _bootstrap(self)


if not getattr(web.Application, "_armenia_platform_bootstrapped", False):
    web.Application.__init__ = _application_init
    web.Application._armenia_platform_bootstrapped = True
