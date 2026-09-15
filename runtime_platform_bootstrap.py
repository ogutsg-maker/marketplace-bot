"""Runtime bridge for the complete Armenia AI Guide architecture.

Loaded from config.py before main creates aiohttp.Application. It keeps the
legacy entrypoint intact while registering the new platform APIs and schema.
"""
from __future__ import annotations

import importlib
from functools import wraps
from aiohttp import web

_original_application_init = web.Application.__init__


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

        # The legacy Telegram onboarding still writes master_skills. Bridge that
        # operation into partner_directions/partner_direction_categories so the
        # new cabinet and admin always see the same approved structure.
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
                    import logging
                    logging.getLogger(__name__).exception("Partner direction bridge failed for %s", user_id)
                return result
            db.set_master_categories = bridged_set_master_categories
            db._armenia_direction_bridge = True

        from partner_directions_api import register_partner_direction_routes
        register_partner_direction_routes(app, db=db, bot=bot)
        from client_api import register_client_routes
        register_client_routes(app, ai)
        from admin_ai_api import register_admin_ai_routes
        register_admin_ai_routes(app, ai, bot=bot)

        app["platform_bootstrap_ready"] = True
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Complete platform bootstrap failed")
        raise


def _application_init(self, *args, **kwargs):
    _original_application_init(self, *args, **kwargs)
    _bootstrap(self)


if not getattr(web.Application, "_armenia_platform_bootstrapped", False):
    web.Application.__init__ = _application_init
    web.Application._armenia_platform_bootstrapped = True
