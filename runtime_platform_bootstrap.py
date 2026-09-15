"""Runtime bridge for the complete Armenia AI Guide architecture.

Loaded from config.py before main() creates aiohttp.Application. It keeps the
legacy entrypoint intact while registering the new platform APIs and schema.
"""
from __future__ import annotations

import importlib
from aiohttp import web

_original_application_init = web.Application.__init__
_patched = False


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

        try:
            from partner_directions_api import register_partner_direction_routes
            register_partner_direction_routes(app, db=db, bot=bot)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Partner directions bootstrap failed")
            raise

        try:
            from client_api import register_client_routes
            register_client_routes(app, ai)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Client AI bootstrap failed")
            raise

        try:
            from admin_ai_api import register_admin_ai_routes
            register_admin_ai_routes(app, ai, bot=bot)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Admin AI bootstrap failed")
            raise

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
