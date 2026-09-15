"""Force a fresh admin WebApp document in Telegram WebView."""
from __future__ import annotations

from aiohttp import web

# config.py imports runtime_platform_bootstrap first. Capture the already
# wrapped Application.__init__ so its platform routes/middleware still run.
_previous_application_init = web.Application.__init__


async def _admin_entry(request: web.Request):
    return web.HTTPFound('/admin_v2.html')


def _application_init(self, *args, **kwargs):
    _previous_application_init(self, *args, **kwargs)
    try:
        self.router.add_get('/admin.html', _admin_entry)
    except Exception:
        pass


if not getattr(web.Application, '_armenia_admin_cache_bootstrapped', False):
    web.Application.__init__ = _application_init
    web.Application._armenia_admin_cache_bootstrapped = True
