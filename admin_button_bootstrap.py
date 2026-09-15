"""Admin-only Telegram button bootstrap for Armenia AI Guide."""
from __future__ import annotations
import asyncio
import importlib
import logging

logger = logging.getLogger(__name__)
_INSTALLED = False


def _handler_list(router):
    return list(getattr(getattr(router, "message", None), "handlers", []) or [])


def _find(router, name):
    for h in _handler_list(router):
        cb = getattr(h, "callback", None)
        if getattr(cb, "__name__", "") == name:
            return h
    return None


async def _admin_keyboard(main, message):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import WebAppInfo
    from config import ADMIN_ID, WEBAPP_BASE_URL
    if int(message.from_user.id) != int(ADMIN_ID):
        return
    b = InlineKeyboardBuilder()
    b.button(text="🔐 Ադմին-պանել / Админ-панель", web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL.rstrip('/')}/admin.html"))
    b.adjust(1)
    await message.answer("🔐 Ձեր ադմինիստրատորի վահանակը / Ваша панель администратора:", reply_markup=b.as_markup())
    try:
        from aiogram.types import MenuButtonWebApp
        await main.bot.set_chat_menu_button(
            chat_id=message.from_user.id,
            menu_button=MenuButtonWebApp(text="🔐 Ադմին-պանել", web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL.rstrip('/')}/admin.html")),
        )
    except Exception:
        logger.exception("Could not set admin chat menu button")


def _patch(main):
    global _INSTALLED
    if _INSTALLED:
        return True
    router = getattr(main, "router", None)
    if router is None:
        return False

    start_h = _find(router, "cmd_start")
    if start_h is not None and not getattr(start_h.callback, "_admin_wrapped", False):
        original = start_h.callback
        async def wrapped_start(message, command, state):
            await original(message, command, state)
            await _admin_keyboard(main, message)
        wrapped_start._admin_wrapped = True
        start_h.callback = wrapped_start

    panel_h = _find(router, "cmd_admin_panel")
    if panel_h is not None and not getattr(panel_h.callback, "_admin_wrapped", False):
        original_panel = panel_h.callback
        async def wrapped_panel(message):
            from config import ADMIN_ID
            if int(message.from_user.id) != int(ADMIN_ID):
                return
            await original_panel(message)
        wrapped_panel._admin_wrapped = True
        panel_h.callback = wrapped_panel

    if start_h is not None:
        _INSTALLED = True
        logger.info("✅ Admin-only Telegram WebApp button installed")
        return True
    return False


def install_async():
    async def waiter():
        main = importlib.import_module("__main__")
        for _ in range(300):
            if _patch(main):
                return
            await asyncio.sleep(0.1)
        logger.error("❌ Admin button bootstrap timeout")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(waiter())
    except RuntimeError:
        import threading
        def runner():
            import time
            main = importlib.import_module("__main__")
            for _ in range(600):
                if _patch(main):
                    return
                time.sleep(0.1)
            logger.error("❌ Admin button bootstrap timeout")
        threading.Thread(target=runner, daemon=True, name="admin-button-bootstrap").start()

install_async()
