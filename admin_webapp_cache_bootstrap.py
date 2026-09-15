"""Force a fresh admin WebApp document in Telegram WebView.

Telegram may keep an older static admin.html document. The /admin_panel button
continues to point at /admin.html, while this bootstrap transparently redirects
that exact URL to a small cache-busting loader page.
"""
from __future__ import annotations

from aiohttp import web

try:
    from runtime_platform_bootstrap import _original_application_init
except Exception:
    _original_application_init = web.Application.__init__


async def _admin_entry(request: web.Request):
    return web.HTTPFound('/admin_v2.html')


def _application_init(self, *args, **kwargs):
    _original_application_init(self, *args, **kwargs)
    try:
        self.router.add_get('/admin.html', _admin_entry)
    except Exception:
        pass


if not getattr(web.Application, '_armenia_admin_cache_bootstrapped', False):
    web.Application.__init__ = _application_init
    web.Application._armenia_admin_cache_bootstrapped = True
