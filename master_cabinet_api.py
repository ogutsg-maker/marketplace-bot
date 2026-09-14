"""Partner cabinet API for the clean Armenia AI Guide architecture.

Uses the new platform tables and does not depend on legacy orders/bids.
"""
from __future__ import annotations

import json
import os
import secrets
from decimal import Decimal
from aiohttp import web

from platform_db import (
    get_partner_by_user,
    update_partner,
    rows,
    one,
    execute,
    approved_partner_categories,
)
from telegram_webapp_auth import (
    validate_telegram_webapp_init_data,
    TelegramWebAppAuthError,
)
from booking_schema import ensure_booking_schema


def _json_response(data, status=200):
    return web.json_response(data, status=status)


def _error(code, status=400):
    return _json_response({"ok": False, "error": code}, status=status)


def _uid(request):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": "telegram_init_data_required"}),
            content_type="application/json",
        )
    try:
        data = validate_telegram_webapp_init_data(
            raw,
            request.app.get("stage3_bot_token") or os.getenv("BOT_TOKEN", ""),
        )
        return int(data["id"])
    except (TelegramWebAppAuthError, ValueError, TypeError, KeyError) as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"ok": False, "error": str(exc)}),
            content_type="application/json",
        )


def _partner(request):
    uid = _uid(request)
    partner = get_partner_by_user(uid)
    if not partner:
        raise web.HTTPForbidden(
            text=json.dumps({"ok": False, "error": "partner_registration_required"}),
            content_type="application/json",
        )
    if partner.get("status") in ("blocked", "suspended", "deleted"):
        raise web.HTTPForbidden(
            text=json.dumps({"ok": False, "error": "partner_blocked"}),
            content_type="application/json",
        )
    return uid, partner


def _json_value(value):
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except Exception:
            return json.dumps({"value": value}, ensure_ascii=False)
    return json.dumps(value or {}, ensure_ascii=False)


def _num(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def dashboard(request):
    uid, partner = _partner(request)
    directions = rows(
        """
        SELECT pd.id, pd.master_category_id, pd.status, pd.rejection_reason,
               m.name_am, m.name_ru,
               COUNT(DISTINCT pdc.category_id) AS category_count,
               COUNT(DISTINCT d.id) AS document_count
        FROM partner_directions pd
        JOIN master_categories m ON m.id = pd.master_category_id
        LEFT JOIN partner_direction_categories pdc
               ON pdc.partner_direction_id = pd.id
        LEFT JOIN partner_verification_documents d
               ON d.partner_direction_id = pd.id
        WHERE pd.partner_id = %s AND pd.status <> 'deleted'
        GROUP BY pd.id, m.id
        ORDER BY pd.id
        """,
        (partner["id"],),
    )

    service_count = one(
        "SELECT COUNT(*) AS n FROM services WHERE partner_id=%s AND status<>'deleted'",
        (partner["id"],),
    )["n"]

    object_count = one(
        "SELECT COUNT(*) AS n FROM partner_objects WHERE partner_id=%s",
        (partner["id"],),
    )["n"]

    booking_count = one(
        """
        SELECT COUNT(*) AS n
        FROM bookings
        WHERE partner_id=%s AND status NOT IN ('cancelled','expired')
        """,
        (partner["id"],),
    )["n"]

    return _json_response({
        "ok": True,
        "partner": partner,
        "directions": directions,
        "service_count": service_count,
        "object_count": object_count,
        "booking_count": booking_count,
    })


async def categories(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": approved_partner_categories(partner["id"]),
    })


async def service_categories(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": rows(
            """
            SELECT id, master_category_id, name_am, name_ru, slug
            FROM categories
            WHERE is_active=TRUE
            ORDER BY master_category_id, id
            """
        ),
    })


async def services(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": rows(
            """
            SELECT s.*,
                   c.name_am AS category_name_am,
                   c.name_ru AS category_name_ru
            FROM services s
            LEFT JOIN categories c ON c.id=s.category_id
            WHERE s.partner_id=%s AND s.status<>'deleted'
            ORDER BY s.created_at DESC
            """,
            (partner["id"],),
        ),
    })


async def create_service(request):
    uid, partner = _partner(request)
    data = await request.json()
    category_id = int(data.get("category_id") or 0)
    subcategory_id = data.get("subcategory_id")
    if not category_id:
        return _error("category_id_required")

    allowed = one(
        """
        SELECT c.id
        FROM categories c
        JOIN partner_direction_categories pdc
          ON pdc.category_id=c.id
        JOIN partner_directions pd
          ON pd.id=pdc.partner_direction_id
         AND pd.status='approved'
        WHERE c.id=%s AND pd.partner_id=%s
        LIMIT 1
        """,
        (category_id, partner["id"]),
    )
    if not allowed:
        return _error("direction_not_approved", 403)

    service = execute(
        """
        INSERT INTO services(
            partner_id, category_id, subcategory_id, name, description,
            price, currency, duration_minutes, status, data_json
        )
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"],
            category_id,
            int(subcategory_id) if subcategory_id else None,
            str(data.get("name") or "Նոր ծառայություն"),
            str(data.get("description") or ""),
            data.get("price"),
            str(data.get("currency") or "AMD"),
            data.get("duration_minutes"),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    return _json_response({"ok": True, "service": service})


async def service_details(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    service = one(
        "SELECT * FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    )
    if not service:
        return _error("service_not_found", 404)

    return _json_response({
        "ok": True,
        "service": service,
        "packages": rows(
            "SELECT * FROM service_packages WHERE service_id=%s ORDER BY id",
            (service_id,),
        ),
        "options": rows(
            "SELECT * FROM service_options WHERE service_id=%s ORDER BY id",
            (service_id,),
        ),
        "schedule": rows(
            "SELECT * FROM service_schedule WHERE service_id=%s ORDER BY weekday",
            (service_id,),
        ),
    })


async def add_package(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    if not one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    ):
        return _error("service_not_found", 404)

    item = execute(
        """
        INSERT INTO service_packages(service_id,name,price,currency,description)
        VALUES(%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            service_id,
            str(data.get("name") or ""),
            data.get("price", 0),
            data.get("currency", "AMD"),
            str(data.get("description") or ""),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def add_option(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    if not one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    ):
        return _error("service_not_found", 404)

    item = execute(
        """
        INSERT INTO service_options(service_id,name,price_delta,currency)
        VALUES(%s,%s,%s,%s)
        RETURNING *
        """,
        (
            service_id,
            str(data.get("name") or ""),
            data.get("price_delta", 0),
            data.get("currency", "AMD"),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def schedule(request):
    uid, partner = _partner(request)
    service_id = int(request.match_info["service_id"])
    data = await request.json()
    if not one(
        "SELECT id FROM services WHERE id=%s AND partner_id=%s",
        (service_id, partner["id"]),
    ):
        return _error("service_not_found", 404)

    item = execute(
        """
        INSERT INTO service_schedule(
            service_id,weekday,start_time,end_time,is_available
        )
        VALUES(%s,%s,%s,%s,%s)
        ON CONFLICT(service_id,weekday)
        DO UPDATE SET
            start_time=EXCLUDED.start_time,
            end_time=EXCLUDED.end_time,
            is_available=EXCLUDED.is_available
        RETURNING *
        """,
        (
            service_id,
            int(data.get("weekday", 0)),
            data.get("start_time") or None,
            data.get("end_time") or None,
            bool(data.get("is_available", True)),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def settings(request):
    uid, partner = _partner(request)
    if request.method == "GET":
        return _json_response({"ok": True, "partner": partner})

    data = await request.json()
    updated = update_partner(
        partner["id"],
        contact_share_policy=str(
            data.get("contact_share_policy")
            or partner.get("contact_share_policy")
            or "after_booking"
        ),
        profile_json=data.get(
            "profile_json",
            partner.get("profile_json") or {},
        ),
    )
    return _json_response({"ok": True, "partner": updated})


async def locations(request):
    uid, partner = _partner(request)
    if request.method == "GET":
        return _json_response({
            "ok": True,
            "items": rows(
                "SELECT * FROM partner_locations WHERE partner_id=%s ORDER BY id",
                (partner["id"],),
            ),
        })

    data = await request.json()
    item = execute(
        """
        INSERT INTO partner_locations(
            partner_id,marz,city,village,address,location_type,data_json
        )
        VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"],
            data.get("marz"),
            data.get("city"),
            data.get("village"),
            data.get("address"),
            data.get("location_type", "fixed"),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def objects(request):
    uid, partner = _partner(request)

    if request.method == "GET":
        return _json_response({
            "ok": True,
            "items": rows(
                """
                SELECT *
                FROM partner_objects
                WHERE partner_id=%s
                ORDER BY id
                """,
                (partner["id"],),
            ),
        })

    data = await request.json()
    name = str(data.get("object_name") or data.get("name") or "").strip()
    if not name:
        return _error("object_name_required")

    item = execute(
        """
        INSERT INTO partner_objects(
            partner_id,object_name,address,city,marz,data_json
        )
        VALUES(%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING *
        """,
        (
            partner["id"],
            name,
            data.get("address"),
            data.get("city"),
            data.get("marz"),
            _json_value(data.get("data_json")),
        ),
        True,
    )
    return _json_response({"ok": True, "item": item})


async def object_delete(request):
    uid, partner = _partner(request)
    object_id = int(request.match_info["object_id"])
    deleted = execute(
        """
        DELETE FROM partner_objects
        WHERE id=%s AND partner_id=%s
        RETURNING id
        """,
        (object_id, partner["id"]),
        True,
    )
    if not deleted:
        return _error("object_not_found", 404)
    return _json_response({"ok": True, "deleted_id": deleted["id"]})


async def bookings(request):
    uid, partner = _partner(request)
    items = rows(
        """
        SELECT b.*,
               u.full_name AS client_name,
               u.username AS client_username
        FROM bookings b
        LEFT JOIN users u ON u.telegram_id=b.client_id
        WHERE b.partner_id=%s
        ORDER BY b.updated_at DESC
        """,
        (partner["id"],),
    )
    return _json_response({"ok": True, "items": items})


async def booking_details(request):
    uid, partner = _partner(request)
    booking_id = int(request.match_info["booking_id"])
    booking = one(
        """
        SELECT b.*,
               u.full_name AS client_name,
               u.username AS client_username
        FROM bookings b
        LEFT JOIN users u ON u.telegram_id=b.client_id
        WHERE b.id=%s AND b.partner_id=%s
        """,
        (booking_id, partner["id"]),
    )
    if not booking:
        return _error("booking_not_found", 404)

    return _json_response({
        "ok": True,
        "booking": booking,
        "payments": rows(
            "SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC",
            (booking_id,),
        ),
        "checkin": one(
            "SELECT * FROM booking_checkins WHERE booking_id=%s",
            (booking_id,),
        ),
    })


async def booking_status(request):
    uid, partner = _partner(request)
    booking_id = int(request.match_info["booking_id"])
    data = await request.json()
    status = str(data.get("status") or "").strip()

    allowed = {
        "confirmed": {"pending", "paid"},
        "completed": {"confirmed", "checked_in"},
        "cancelled": {
            "pending", "awaiting_payment", "paid", "confirmed"
        },
    }
    current = one(
        "SELECT status FROM bookings WHERE id=%s AND partner_id=%s",
        (booking_id, partner["id"]),
    )
    if not current:
        return _error("booking_not_found", 404)

    if status not in allowed or current["status"] not in allowed[status]:
        return _error("invalid_status_transition")

    item = execute(
        """
        UPDATE bookings
        SET status=%s,updated_at=NOW()
        WHERE id=%s AND partner_id=%s
        RETURNING *
        """,
        (status, booking_id, partner["id"]),
        True,
    )

    if status == "cancelled":
        reason = str(data.get("reason") or "Отменено партнёром")
        execute(
            """
            INSERT INTO booking_cancellations(
                booking_id,cancelled_by,reason
            )
            VALUES(%s,%s,%s)
            """,
            (booking_id, "partner", reason),
        )

    return _json_response({"ok": True, "booking": item})


async def checkin(request):
    uid, partner = _partner(request)
    data = await request.json()
    booking_id = data.get("booking_id")
    token = str(data.get("token") or "").strip()

    if booking_id:
        booking_id = int(booking_id)
        booking = one(
            "SELECT * FROM bookings WHERE id=%s AND partner_id=%s",
            (booking_id, partner["id"]),
        )
        if not booking:
            return _error("booking_not_found", 404)

        if booking["status"] not in ("paid", "confirmed", "checked_in"):
            return _error("booking_not_ready_for_checkin")

        existing = one(
            "SELECT * FROM booking_checkins WHERE booking_id=%s",
            (booking_id,),
        )
        if existing:
            return _json_response({"ok": True, "checkin": existing})

        new_token = secrets.token_urlsafe(32)
        item = execute(
            """
            INSERT INTO booking_checkins(booking_id,token)
            VALUES(%s,%s)
            RETURNING *
            """,
            (booking_id, new_token),
            True,
        )
        return _json_response({"ok": True, "checkin": item})

    if token:
        check = one(
            """
            SELECT bc.*,b.partner_id,b.status AS booking_status
            FROM booking_checkins bc
            JOIN bookings b ON b.id=bc.booking_id
            WHERE bc.token=%s AND b.partner_id=%s
            """,
            (token, partner["id"]),
        )
        if not check:
            return _error("checkin_token_not_found", 404)
        if check["status"] != "active":
            return _error("checkin_token_not_active")

        execute(
            """
            UPDATE booking_checkins
            SET status='used',checked_in_at=NOW(),checked_in_by=%s
            WHERE id=%s
            """,
            (uid, check["id"]),
        )
        booking = execute(
            """
            UPDATE bookings
            SET status='checked_in',updated_at=NOW()
            WHERE id=%s AND partner_id=%s
            RETURNING *
            """,
            (check["booking_id"], partner["id"]),
            True,
        )
        return _json_response({"ok": True, "booking": booking})

    return _error("booking_id_or_token_required")


async def history(request):
    uid, partner = _partner(request)
    return _json_response({
        "ok": True,
        "items": rows(
            """
            SELECT b.id,b.status,b.agreed_price,b.currency,
                   b.service_name,b.scheduled_at,b.updated_at
            FROM bookings b
            WHERE b.partner_id=%s
            ORDER BY b.updated_at DESC
            LIMIT 100
            """,
            (partner["id"],),
        ),
    })


async def finance(request):
    uid, partner = _partner(request)
    result = one(
        """
        SELECT
            COALESCE(SUM(agreed_price),0) AS gross,
            COALESCE(SUM(commission_amount),0) AS commission,
            COALESCE(SUM(partner_amount),0) AS partner_amount
        FROM bookings
        WHERE partner_id=%s AND status IN ('paid','confirmed','checked_in','completed')
        """,
        (partner["id"],),
    )
    ledger = rows(
        """
        SELECT *
        FROM partner_financial_ledger
        WHERE partner_id=%s
        ORDER BY created_at DESC
        LIMIT 100
        """,
        (partner["id"],),
    )
    return _json_response({
        "ok": True,
        "gross": _num(result["gross"]),
        "commission": _num(result["commission"]),
        "partner_amount": _num(result["partner_amount"]),
        "ledger": ledger,
    })


def register_master_cabinet_routes(app, db, bot=None):
    # Extend the clean schema once at startup. Safe because every statement
    # uses IF NOT EXISTS.
    ensure_booking_schema()

    app.router.add_get("/api/master/{id}/dashboard", dashboard)
    app.router.add_get("/api/master/{id}/categories", categories)
    app.router.add_get("/api/master/{id}/service-categories", service_categories)

    app.router.add_get("/api/master/{id}/services", services)
    app.router.add_post("/api/master/{id}/services", create_service)
    app.router.add_get(
        "/api/master/{id}/services/{service_id}/details",
        service_details,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/packages",
        add_package,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/options",
        add_option,
    )
    app.router.add_post(
        "/api/master/{id}/services/{service_id}/schedule",
        schedule,
    )

    app.router.add_route("*", "/api/master/{id}/settings", settings)
    app.router.add_route("*", "/api/master/{id}/locations", locations)

    app.router.add_get("/api/master/{id}/objects", objects)
    app.router.add_post("/api/master/{id}/objects", objects)
    app.router.add_delete(
        "/api/master/{id}/objects/{object_id}",
        object_delete,
    )

    app.router.add_get("/api/master/{id}/bookings", bookings)
    app.router.add_get(
        "/api/master/{id}/bookings/{booking_id}",
        booking_details,
    )
    app.router.add_post(
        "/api/master/{id}/bookings/{booking_id}/status",
        booking_status,
    )
    app.router.add_post("/api/master/{id}/checkin", checkin)

    app.router.add_get("/api/master/{id}/history", history)
    app.router.add_get("/api/master/{id}/finance", finance)
