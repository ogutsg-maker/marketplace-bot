"""
marketplace_bot — Гибридный маркетплейс локальных услуг в Армении.
AI-диспетчер (Groq) + Idram-платежи + анонимный чат с модерацией + торги.

Точка входа: совмещённый aiohttp-сервер (API + статика Web Apps)
             + aiogram long-polling бот.
"""
import os
import json
import asyncio
import logging
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import CommandStart, Command, CommandObject, Filter
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import WebAppInfo, MenuButtonWebApp

from config import (
    BOT_TOKEN, ADMIN_ID, WEBAPP_BASE_URL,
)
from database import DatabaseManager
from ai_dispatcher import AIDispatcher
from billing import BillingManager
from partner_registration_ai import extract as extract_partner_profile, match_subcategories, missing_question
from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data
from stage3_partner_verification import register_stage3_routes

try:
    from master_cabinet_api import register_master_cabinet_routes
except ImportError:
    register_master_cabinet_routes = None
from states import RegistrationStates, BiddingStates, CodeEntryStates
from keyboards import (
    get_role_keyboard, get_language_keyboard, get_city_keyboard,
    get_master_categories_keyboard,
    get_categories_keyboard, get_master_accept_keyboard,
    get_bid_actions_keyboard, get_close_deal_keyboard,
    get_rating_keyboard, get_master_menu_keyboard, get_client_menu_keyboard,
)

# ─── Инициализация ──────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

db = DatabaseManager()
ai = AIDispatcher()
billing_mgr = BillingManager(db)

BASE_DIR = Path(__file__).resolve().parent
WEB_APPS_DIR = BASE_DIR / "web_apps"


def webapp_url(path: str) -> str:
    return f"{WEBAPP_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def get_app_keyboard(path: str, text: str):
    builder = InlineKeyboardBuilder()
    builder.button(text=text, web_app=WebAppInfo(url=webapp_url(path)))
    builder.adjust(1)
    return builder.as_markup()


def get_welcome_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Բացել Armenia AI Guide / Открыть Armenia AI Guide",
        web_app=WebAppInfo(url=webapp_url("welcome.html")),
    )
    builder.adjust(1)
    return builder.as_markup()


def get_client_app_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👤 Բացել հաճախորդի բաժինը / Открыть раздел клиента",
        web_app=WebAppInfo(url=webapp_url("client.html")),
    )
    builder.adjust(1)
    return builder.as_markup()


def get_partner_app_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🏢 Բացել գործընկերոջ բաժինը / Открыть раздел партнёра",
        web_app=WebAppInfo(url=webapp_url("partner.html")),
    )
    builder.adjust(1)
    return builder.as_markup()


async def api_webapp_set_role(request: web.Request):
    """Set the selected role from the Welcome WebApp using Telegram initData."""
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        return web.json_response({"ok": False, "error": "telegram_init_data_required"}, status=401)
    try:
        telegram_user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        uid = int(telegram_user["id"])
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    role = str(data.get("role") or "").strip().lower()
    if role not in {"client", "master"}:
        return web.json_response({"ok": False, "error": "invalid_role"}, status=400)

    user = db.get_user(uid)
    if not user:
        db.register_user(uid, telegram_user.get("username") or f"user_{uid}", telegram_user.get("first_name") or "")

    db.update_user_field(uid, "role", role)
    lang = str(data.get("lang") or "").strip().lower()
    if lang in {"hy", "ru", "en"}:
        db.update_user_field(uid, "lang", lang)
    return web.json_response({"ok": True, "role": role, "telegram_id": uid})


async def api_webapp_partner_start(request: web.Request):
    """Start partner onboarding directly from WebApp without relying on tg.sendData()."""
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw:
        return web.json_response({"ok": False, "error": "telegram_init_data_required"}, status=401)
    try:
        telegram_user = validate_telegram_webapp_init_data(raw, BOT_TOKEN)
        uid = int(telegram_user["id"])
    except (TelegramWebAppAuthError, KeyError, TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc) or "invalid_telegram_init_data"}, status=401)

    user = db.get_user(uid)
    if not user:
        db.register_user(
            uid,
            telegram_user.get("username") or f"user_{uid}",
            telegram_user.get("first_name") or "",
        )
        user = db.get_user(uid) or {}

    db.update_user_field(uid, "role", "master")
    lang = (user or {}).get("lang") or "hy"

    # Use the same aiogram FSM storage as normal Telegram messages.
    key = StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)
    state = FSMContext(storage=storage, key=key)
    await state.clear()
    await state.set_state(RegistrationStates.choosing_city)
    await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)

    await bot.send_message(
        uid,
        t(lang,
          "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Ես կճանաչեմ ուղղությունը, ենթաուղղությունները և ծառայությունները։",
          "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и какие у них цены. Я сам определю направление, подкатегории и услуги.",
          "🏢 <b>Let’s register your business</b>\n\nTell me naturally what the business is called, where it is located, what services you offer and their prices. I will determine the direction, subcategories and services."),
        parse_mode=ParseMode.HTML,
    )
    return web.json_response({"ok": True, "started": True, "telegram_id": uid})



# ─── TELEGRAM WEB APP AUTH / PARTNER GATING ─────────────────────

@web.middleware
async def telegram_webapp_auth_middleware(request: web.Request, handler):
    """Authenticate Telegram WebApp requests and gate partner cabinet by approval status."""
    path = request.path

    if not path.startswith("/api/master/"):
        return await handler(request)

    if request.method == "OPTIONS":
        return await handler(request)

    raw_init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw_init_data:
        return web.json_response(
            {"ok": False, "error": "telegram_init_data_required"},
            status=401,
        )

    try:
        telegram_user = validate_telegram_webapp_init_data(
            raw_init_data,
            BOT_TOKEN,
        )
        telegram_id = int(telegram_user["id"])
        route_user_id = int(path.split("/")[3])
    except (
        TelegramWebAppAuthError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return web.json_response(
            {
                "ok": False,
                "error": str(exc) or "invalid_telegram_init_data",
            },
            status=401,
        )

    if telegram_id != route_user_id:
        return web.json_response(
            {
                "ok": False,
                "error": "telegram_user_mismatch",
            },
            status=403,
        )

    request["telegram_user"] = telegram_user
    request["telegram_user_id"] = telegram_id

    parts = path.split("/", 4)
    tail = parts[4] if len(parts) > 4 else ""

    # Эти endpoints доступны до одобрения партнёра.
    if tail in (
        "registration-status",
        "register",
        "documents",
        "documents/upload",
    ):
        return await handler(request)

    try:
        partner = db.get_partner_by_user(telegram_id)
    except Exception:
        logger.exception("Partner status lookup failed for %s", telegram_id)
        partner = None

    if not partner:
        return web.json_response(
            {
                "ok": False,
                "error": "partner_registration_required",
                "status": "not_registered",
            },
            status=403,
        )

    status = str(partner.get("status") or "pending")

    if status != "approved":
        return web.json_response(
            {
                "ok": False,
                "error": "partner_not_approved",
                "status": status,
                "verification_status": partner.get("verification_status"),
            },
            status=403,
        )

    return await handler(request)


async def api_partner_registration_status(request: web.Request):
    uid = int(request.match_info["id"])
    partner = db.get_partner_by_user(uid)

    if not partner:
        return web.json_response(
            {"ok": True, "registered": False, "status": "not_registered"}
        )

    return web.json_response({
        "ok": True,
        "registered": True,
        "partner_id": partner.get("id"),
        "status": partner.get("status"),
        "verification_status": partner.get("verification_status"),
        "business_name": partner.get("business_name") or "",
        "business_description": partner.get("business_description") or "",
        "rejection_reason": partner.get("rejection_reason") or "",
    })


async def api_partner_register(request: web.Request):
    uid = int(request.match_info["id"])

    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "invalid_json"},
            status=400,
        )

    business_name = str(data.get("business_name") or "").strip()[:200]
    business_description = str(
        data.get("business_description") or ""
    ).strip()[:3000]

    if len(business_name) < 2:
        return web.json_response(
            {"ok": False, "error": "business_name_required"},
            status=400,
        )

    partner = db.get_partner_by_user(uid)

    if partner:
        status = str(partner.get("status") or "pending")

        if status == "approved":
            return web.json_response({
                "ok": True,
                "registered": True,
                "status": status,
                "partner_id": partner.get("id"),
            })

        if status in ("pending", "under_review"):
            return web.json_response({
                "ok": True,
                "registered": True,
                "status": status,
                "partner_id": partner.get("id"),
            })

        partner_id = partner.get("id")
        db.update_partner(
            partner_id,
            business_name=business_name,
            business_description=business_description,
            status="pending",
            verification_status="not_submitted",
        )
    else:
        partner_id = db.create_partner(uid)
        db.update_partner(
            partner_id,
            business_name=business_name,
            business_description=business_description,
            status="pending",
            verification_status="not_submitted",
        )

    return web.json_response({
        "ok": True,
        "registered": True,
        "status": "pending",
        "partner_id": partner_id,
        "message": "Դիմումը ուղարկված է ադմինիստրատորի ստուգմանը։",
    })


async def serve_index(request: web.Request):
    """Публичная главная страница Web App."""
    index_file = WEB_APPS_DIR / "index.html"

    if not index_file.is_file():
        logger.error("Главная страница не найдена: %s", index_file)
        return web.json_response(
            {"ok": False, "error": "index.html_not_found"},
            status=500,
        )

    return web.FileResponse(index_file)


async def health(request: web.Request):
    return web.json_response({"ok": True})


# ─── Фильтры ────────────────────────────────────────────────────

class IsAdmin(Filter):
    async def __call__(self, message: types.Message) -> bool:
        return message.from_user.id == ADMIN_ID


# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────

def t(lang: str, hy: str, ru: str, en: str) -> str:
    """Мультиязычный текст по ключу lang."""
    return {"hy": hy, "ru": ru, "en": en}.get(lang, ru)


async def save_voice_temp(voice: types.Voice) -> str:
    """Скачивает голосовое сообщение во временный файл."""
    file = await bot.get_file(voice.file_id)
    suffix = ".ogg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        await bot.download_file(file.file_path, tmp.name)
        return tmp.name


@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    stats = db.get_admin_stats()
    await message.answer(
        f"👑 **АДМИН-ПАНЕЛЬ**\n\n"
        f"👥 Пользователей: {stats.get('total_users', 0)}\n"
        f"🛠 Мастеров: {stats.get('total_masters', 0)} (✅ {stats.get('verified_masters', 0)})\n"
        f"📦 Заказов: {stats.get('total_orders', 0)}\n"
        f"✅ Выполнено: {stats.get('completed_orders', 0)}\n"
        f"💰 Комиссия: {float(stats.get('total_commission', 0)):,.0f} ֏\n"
        f"⚖️ Споров: {stats.get('open_disputes', 0)}",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("admin_panel"))
async def cmd_admin_panel(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👑 Открыть панель",
        web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/admin.html"),
    )
    await message.answer("Админ-панель:", reply_markup=builder.as_markup())


@router.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    db.update_user_field(uid, "role", None)
    db.update_user_field(uid, "city", None)
    await state.clear()
    await message.answer("🔄 Профиль сброшен. Напишите /start для повторной настройки.")
# ═══════════════════════════════════════════════════════════════
# 1. РЕГИСТРАЦИЯ И /START
# ═══════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    username = message.from_user.username or f"user_{uid}"
    full_name = message.from_user.full_name or ""

    # --- Deep-link: закрытие сделки по QR ---
    if command.args and command.args.startswith("close_"):
        parts = command.args.split("_")
        if len(parts) >= 3:
            deal_id, secure_code = parts[1], parts[2]
            result = db.verify_and_close_deal(deal_id, secure_code, uid)
            if result == "ok":
                deal = db.get_deal(deal_id)
                if deal and deal.get("client_id"):
                    await bot.send_message(
                        deal["client_id"],
                        f"✅ **Сделка #{deal_id} закрыта!**\n"
                        f"Мастер подтвердил выполнение.\n\n"
                        f"Пожалуйста, оцените работу мастера:",
                        reply_markup=get_rating_keyboard(deal_id, uid),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                await message.answer(
                    f"✅ **Сделка #{deal_id} успешно закрыта!**\n"
                    f"Клиент получил уведомление.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            elif result == "already_closed":
                await message.answer("ℹ️ Эта сделка уже закрыта.")
            else:
                await message.answer("❌ Неверный код верификации.")
            return

    # --- Регистрация ---
    db.register_user(uid, username, full_name)
    user = db.get_user(uid)

    if user and user.get("role") is not None:
        role = user["role"]
        if role == "master":
            await message.answer(
                "🏢 Armenia AI Guide — Ձեր գործընկերոջ բաժինը\n\nԲացեք ձեր գործընկերոջ էջը՝ շարունակելու աշխատանքը:",
                reply_markup=get_partner_app_keyboard(),
            )
        else:
            await message.answer(
                "👤 Armenia AI Guide — Ձեր AI օգնականը\n\nԲացեք հաճախորդի էջը՝ ծառայություն գտնելու և ամրագրելու համար:",
                reply_markup=get_client_app_keyboard(),
            )
        return

    # --- Первый вход: всегда красивая Welcome WebApp, без запуска AI-чата. ---
    await state.clear()
    await message.answer(
        "✦ <b>Armenia AI Guide</b>\n"
        "<i>ARMENIA · AI CONCIERGE</i>\n\n"
        "Ձեր AI օգնականը ծառայություններ գտնելու, ընտրելու և ամրագրման հարցերում։\n\n"
        "Սկսելու համար բացեք Armenia AI Guide-ը։ / Откройте Armenia AI Guide, чтобы начать.",
        reply_markup=get_welcome_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("lang_"))
async def process_lang(callback: types.CallbackQuery, state: FSMContext):
    lang = callback.data.replace("lang_", "")
    db.update_user_field(callback.from_user.id, "lang", lang)
    await state.set_state(RegistrationStates.choosing_role)
    await callback.message.edit_text(
        t(lang,
          "Բարև Ձեզ! Ողջունում ենք ԻԻ-Մարկետփլեյսում: 🇦🇲\nԸնտրեք Ձեր դերը:",
          "Здравствуйте! Добро пожаловать на ИИ-Маркетплейс. 🇦🇲\nВыберите вашу роль:",
          "Hello! Welcome to AI Marketplace. 🇦🇲\nChoose your role:"),
        reply_markup=get_role_keyboard(),
    )
    await callback.answer()


@router.callback_query(RegistrationStates.choosing_role, F.data.startswith("role_"))
async def process_role(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")
    chosen = callback.data.replace("role_", "")

    if chosen == "client":
        db.update_user_field(uid, "role", "client")
        await state.clear()
        await callback.message.edit_text(
            t(lang,
              "🎉 Դուք հաճախորդ եք: Նկարագրեք ձեր խնդիրը, և ԻԻ-ն կգտնի վարպետին:",
              "🎉 Вы — Клиент. Опишите задачу, и ИИ найдёт мастера!",
              "🎉 You are a Client. Describe your task and AI will find a master!"),
        )
    elif chosen == "master":
        db.update_user_field(uid, "role", "master")
        await state.set_state(RegistrationStates.choosing_city)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
        await callback.message.edit_text(
            t(lang,
              "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Ես կճանաչեմ ուղղությունը, ենթաուղղությունները և ծառայությունները։",
              "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и какие у них цены. Я сам определю направление, подкатегории и услуги.",
              "🏢 <b>Let’s register your business</b>\n\nTell me naturally what the business is called, where it is located, what services you offer and their prices. I will determine the direction, subcategories and services."),
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(F.web_app_data)
async def handle_webapp_data(message: types.Message, state: FSMContext):
    """Commands sent by role-specific WebApps back to the Telegram bot."""
    try:
        payload = json.loads(message.web_app_data.data or "{}")
    except Exception:
        payload = {"action": message.web_app_data.data}

    action = str(payload.get("action") or "").strip().lower()
    uid = message.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")

    if action == "partner_register":
        db.update_user_field(uid, "role", "master")
        await state.clear()
        await state.set_state(RegistrationStates.choosing_city)
        await state.update_data(partner_onboarding_history=[], partner_profile={}, partner_onboarding_pending_field=None)
        await message.answer(
            t(lang,
              "🏢 <b>Գրանցենք ձեր բիզնեսը</b>\n\nՊատմեք ազատ ձևով՝ ինչպես է կոչվում բիզնեսը, որտեղ է գտնվում, ինչ ծառայություններ եք մատուցում և ինչ գներով։ Ես կճանաչեմ ուղղությունը, ենթաուղղությունները և ծառայությունները։",
              "🏢 <b>Зарегистрируем ваш бизнес</b>\n\nРасскажите свободно: как называется бизнес, где находится, какие услуги вы оказываете и какие у них цены. Я сам определю направление, подкатегории и услуги.",
              "🏢 <b>Let’s register your business</b>\n\nTell me naturally what the business is called, where it is located, what services you offer and their prices. I will determine the direction, subcategories and services."),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "client_open":
        db.update_user_field(uid, "role", "client")
        await state.clear()
        await message.answer(
            t(lang,
              "👤 Բարի գալուստ։ Բացեք հաճախորդի բաժինը։",
              "👤 Добро пожаловать. Откройте раздел клиента.",
              "👤 Welcome. Open the client section."),
            reply_markup=get_client_app_keyboard(),
        )


async def _partner_onboarding_message(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    user = db.get_user(uid) or {}
    lang = user.get("lang", "hy")
    text = (message.text or "").strip()
    if len(text) < 2:
        return

    data = await state.get_data()
    history = data.get("partner_onboarding_history") or []
    pending_field = data.get("partner_onboarding_pending_field")
    previous_profile = data.get("partner_profile") or {}
    history.append({"role": "user", "content": text})

    await bot.send_chat_action(uid, "typing")
    profile = await extract_partner_profile(text, history, db, previous_profile=previous_profile, pending_field=pending_field)

    # Never lose fields already collected in earlier turns.
    merged = dict(previous_profile)
    for key, value in (profile or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    profile = merged

    # If we explicitly asked for one field, the next user message is its answer.
    if pending_field in {"business_name", "city", "district", "direction"} and text:
        profile[pending_field] = text.strip()
    elif pending_field == "services" and text:
        if not profile.get("services"):
            profile["services"] = [{"name": text.strip(), "price": None, "price_type": "unknown"}]
    city_hint = data.get("partner_city_hint")
    if city_hint and not profile.get("city"):
        profile["city"] = city_hint
    # Calculate missing fields ourselves; do not trust an LLM to forget the previous turn.
    required_missing = []
    if not profile.get("business_name"): required_missing.append("business_name")
    if not profile.get("city"): required_missing.append("city")
    if not profile.get("direction"): required_missing.append("direction")
    if not profile.get("services"): required_missing.append("services")
    profile["missing"] = required_missing
    profile["ready"] = not required_missing

    await state.update_data(partner_onboarding_history=history, partner_profile=profile)

    if not profile.get("ready"):
        # If the LLM failed to mark ready but all essential fields are present,
        # let the deterministic check below decide.
        essential = all(profile.get(k) for k in ("business_name", "city", "direction")) and bool(profile.get("services"))
        if not essential:
            question = missing_question(profile, lang)
            next_field = (profile.get("missing") or [None])[0]
            await state.update_data(partner_onboarding_pending_field=next_field)
            history.append({"role": "assistant", "content": question})
            await state.update_data(partner_onboarding_history=history)
            await message.answer(
                t(lang,
                  f"🤖 Ես արդեն հավաքել եմ ձեր ասած տվյալները։ {question}",
                  f"🤖 Я уже собрал то, что вы рассказали. {question}",
                  f"🤖 I have collected the information you gave me. {question}"),
            )
            return

    name = str(profile.get("business_name") or "").strip()[:200]
    city = str(profile.get("city") or "").strip()[:200]
    direction = str(profile.get("direction") or "").strip()[:200]
    description = str(profile.get("description") or "").strip()
    services = profile.get("services") or []

    # Persist the structured profile in the existing partner record.
    partner = db.get_partner_by_user(uid)
    if partner:
        partner_id = partner.get("id")
        db.update_partner(partner_id, business_name=name, business_description=description, status="pending", verification_status="not_submitted")
    else:
        partner_id = db.create_partner(uid)
        db.update_partner(partner_id, business_name=name, business_description=description, status="pending", verification_status="not_submitted")

    db.update_user_field(uid, "city", city)

    # Map AI-selected subcategories into the existing category system.
    category_ids = match_subcategories(db, profile.get("subcategory_names") or [])
    if category_ids:
        try:
            db.set_master_categories(uid, category_ids)
        except Exception:
            logger.exception("Could not save AI-selected partner categories for %s", uid)

    service_lines = []
    for item in services:
        if not isinstance(item, dict):
            continue
        n = str(item.get("name") or "").strip()
        if not n:
            continue
        price = item.get("price")
        if price not in (None, ""):
            service_lines.append(f"• {n} — {price} ֏")
        else:
            service_lines.append(f"• {n}")

    summary = [
        f"🏢 {name}",
        f"📍 {city}",
        f"🧭 {direction}",
    ]
    if profile.get("district"):
        summary.append(f"📌 {profile['district']}")
    if service_lines:
        summary.append("\n🛠 Ծառայություններ / Услуги:\n" + "\n".join(service_lines[:20]))

    await state.clear()
    await message.answer(
        t(lang,
          "✅ Բիզնեսի տվյալները ճանաչեցի և պահպանեցի։\n\n" + "\n".join(summary) + "\n\n📄 Հաջորդ քայլը՝ բիզնեսը հաստատելու փաստաթուղթը ուղարկեք գործընկերոջ բաժնում։ Հայտը կգնա ադմինիստրատորի ստուգմանը։",
          "✅ Я распознал и сохранил данные бизнеса.\n\n" + "\n".join(summary) + "\n\n📄 Следующий шаг — отправьте подтверждающий документ в разделе партнёра. Заявка будет передана администратору на проверку.",
          "✅ I recognized and saved your business information.\n\n" + "\n".join(summary) + "\n\n📄 Next step: upload the verification document in the partner section. Your application will then go to admin review."),
    )


# --- Город (быстрый выбор или текстовый ввод) ---

@router.callback_query(RegistrationStates.choosing_city, F.data.startswith("city_"))
async def process_city_quick(callback: types.CallbackQuery, state: FSMContext):
    city = callback.data.replace("city_", "")
    uid = callback.from_user.id
    db.update_user_field(uid, "city", city)
    await _show_categories_selection(uid, state, callback.message, city)
    await callback.answer()


@router.message(RegistrationStates.choosing_city)
async def process_city_text(message: types.Message, state: FSMContext):
    await _partner_onboarding_message(message, state)


async def _show_categories_selection(uid: int, state: FSMContext, msg, city: str):
    """Показывает мастеру сферы деятельности для выбора специализации."""
    await state.update_data(selected_cats=[])
    await state.set_state(RegistrationStates.choosing_categories)
    
    master_cats = db.get_all_master_categories()
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    await msg.answer(
        f"📍 Քաղաքը գրանցված է / Город сохранен: **{city}**\n\n"
        f"Ընտրեք ուղղությունը / Выберите сферу деятельности:",
        reply_markup=get_master_categories_keyboard(master_cats, lang),
        parse_mode=ParseMode.MARKDOWN,
    )


# --- Обработчики двухуровневых категорий ---

@router.callback_query(RegistrationStates.choosing_categories, F.data.startswith("select_mcat_"))
async def process_select_master_category(callback: types.CallbackQuery, state: FSMContext):
    """ЭТАП 2: Мастер нажал на главную сферу. Показываем подкатегории этой сферы."""
    master_category_id = int(callback.data.replace("select_mcat_", ""))
    await state.update_data(current_master_category_id=master_category_id)
    data = await state.get_data()
    selected = data.get("selected_cats", [])
    
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    subcategories = db.get_subcategories_by_master(master_category_id)
    if not subcategories:
        await callback.answer("⚠️ В этой сфере пока нет подкатегорий!", show_alert=True)
        return
        
    await callback.message.edit_text(
        t(lang,
          "Ընտրեք կոնկրետ ուղղությունները (կարող եք ընտրել մի քանիսը):",
          "Выберите конкретные подкатегории (можно выбрать несколько):",
          "Select specific subcategories (you can choose multiple):"),
        reply_markup=get_categories_keyboard(subcategories, selected, lang)
    )
    await callback.answer()


@router.callback_query(RegistrationStates.choosing_categories, F.data.startswith("mcat_"))
async def toggle_category(callback: types.CallbackQuery, state: FSMContext):
    """ЭТАП 3: Мастер нажимает на конкретную подкатегорию (включение/выключение галочки)."""
    cat_id = int(callback.data.replace("mcat_", ""))
    data = await state.get_data()
    selected = data.get("selected_cats", [])
    current_mcat_id = data.get("current_master_category_id", 1)
    
    if cat_id in selected:
        selected.remove(cat_id)
    else:
        selected.append(cat_id)
        
    await state.update_data(selected_cats=selected)
    
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    cats = db.get_subcategories_by_master(current_mcat_id)
    await callback.message.edit_reply_markup(
        reply_markup=get_categories_keyboard(cats, selected, lang)
    )
    await callback.answer("✅" if cat_id in selected else "❌")


@router.callback_query(RegistrationStates.choosing_categories, F.data == "back_to_mcat")
async def process_back_to_master_categories(callback: types.CallbackQuery, state: FSMContext):
    """Кнопка 'Назад': возвращает мастера от списка услуг к главным сферам."""
    master_cats = db.get_all_master_categories()
    uid = callback.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "ru")
    
    await callback.message.edit_text(
        f"Ընտրեք ուղղությունը / Выберите сферу деятельности:",
        reply_markup=get_master_categories_keyboard(master_cats, lang)
    )
    await callback.answer()


@router.callback_query(RegistrationStates.choosing_categories, F.data == "cats_done")
async def finish_registration(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    data = await state.get_data()
    selected = data.get("selected_cats", [])
    if not selected:
        await callback.answer("⚠️ Выберите хотя бы одну категорию!", show_alert=True)
        return
    db.set_master_categories(uid, selected)
    await state.clear()
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")
    await callback.message.edit_text(
        t(lang,
          "🎉 Գրանցումն ավարտված է: Սպասեք հայտեր:",
          "🎉 Регистрация завершена! Ожидайте заявки.",
          "🎉 Registration complete! Await orders."),
    )
    await callback.answer()
# ═══════════════════════════════════════════════════════════════
# 2. ХЕНДЛЕРЫ КНОПОК МЕНЮ (Reply клавиатура)
# ═══════════════════════════════════════════════════════════════

@router.message(F.text == "📋 Իմ հայտերը / Мои заявки")
async def handle_my_orders(message: types.Message):
    """Мастер: показать подходящие новые заявки из его ленты."""
    uid = message.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")

    feed = db.get_master_feed(uid)
    if not feed:
        await message.answer(
            t(lang,
              "Այս պահին ձեր ուղղություններով նոր հայտեր չկան։",
              "📋 Пока нет новых заявок по вашим направлениям.",
              "📋 No new orders in your categories yet."))
        return

    lines = []
    for o in feed[:5]:
        cat_title = o.get('category_name_am','') or o.get('category_name','') or o.get('category_name_hy','')
        lines.append(
            f"🔔 #{o['id']} · {cat_title}\n"
            f"📍 {o.get('city','')}  ·  {o.get('time_ago','')}\n"
            f"📋 {o.get('summary','')}"
        )

    await message.answer(
        t(lang, "📋 Ձեր հայտերը", "📋 Ваши заявки", "📋 Your orders") + 
        f" ({len(feed)}):\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(F.text == "📊 Պատմություն / История")
async def handle_history(message: types.Message):
    """История: для мастера — заработок и сделки, для клиента — список его заказов."""
    uid = message.from_user.id
    user = db.get_user(uid)
    if not user:
        return
    lang = user.get("lang", "hy")
    role = user.get("role", "client")

    if role == "master":
        try:
            data = db.get_master_history(uid) or {}
        except Exception as exc:
            logger.exception("get_master_history failed for %s", uid)
            data = {"completed_count": 0, "total_earned": 0, "total_commission": 0, "avg_rating": None, "deals": []}
        avg = f"{float(data.get('avg_rating') or 0):.1f}⭐" if data.get("avg_rating") else "—"
        await message.answer(
            t(lang, "📊 Ձեր վիճակագրությունը", "📊 Ваша статистика", "📊 Your stats") +
            f"\n\n"
            f"✅ {t(lang,'Ավարտված','Выполнено','Completed')}: {data['completed_count']}\n"
            f"💰 {t(lang,'Վաստակ','Заработок','Earned')}: {data['total_earned']:,.0f} ֏\n"
            f"🏦 {t(lang,'Ծառայություն','Комиссия','Commission')}: {data['total_commission']:,.0f} ֏\n"
            f"⭐ {t(lang,'Միջին գնահատական','Средний рейтинг','Avg rating')}: {avg}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        orders = db.get_client_orders(uid)
        if not orders:
            await message.answer(
                t(lang, "Դուք դեռ հայտեր չունեք:", "📋 У вас пока нет заказов.", "📋 No orders yet."))
            return
        lines = []
        for o in orders[:5]:
            status_emoji = {"bidding": "🔄", "awaiting_payment": "💳",
                           "booked": "📅", "completed": "✅", "cancelled": "❌"}.get(o.get("status",""), "❓")
            lines.append(f"{status_emoji} #{o['id']} · {o.get('category_name_hy','')} — {o.get('status','')}")
        await message.answer(
            t(lang, "Ձեր պատվերները", "Ваши заказы", "Your orders") +
            f" ({len(orders)}):\n\n" + "\n".join(lines))


@router.message(F.text == "📝 Նոր հայտ / Новая заявка")
async def new_request_prompt(message: types.Message):
    """Клиент нажал кнопку новой заявки."""
    await message.answer(
        "📝 Նկարագրեք ձեր խնդիրը / Опишите вашу задачу / Describe your task:"
    )
# ═══════════════════════════════════════════════════════════════
# 3. ОБРАБОТКА ЗАПРОСОВ КЛИЕНТА (ГОЛОС И ТЕКСТ)
# ═══════════════════════════════════════════════════════════════

@router.message(F.voice)
async def handle_voice(message: types.Message, state: FSMContext):
    """Голосовой запрос → Whisper STT → ИИ-анализ структуры заказа."""
    uid = message.from_user.id
    user = db.get_user(uid)
    if not user or user.get("role") != "client":
        return

    await bot.send_chat_action(uid, "typing")
    tmp_path = await save_voice_temp(message.voice)
    transcript = await ai.transcribe_voice(tmp_path)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass

    if transcript.startswith("["):
        await message.answer(f"⚠️ {transcript}")
        return

    await message.reply(f"🎤 Տրանսկրիպցիա / Транскрипция:\n{transcript}")
    await _process_client_request(message, transcript, state)


@router.message()
async def handle_text_request(message: types.Message, state: FSMContext):
    """Текстовый запрос клиента / мастера → ИИ-анализ или чат."""
    current = await state.get_state()
    if current is not None:
        return

    if message.text and message.text.startswith("/"):
        return

    uid = message.from_user.id
    user = db.get_user(uid)
    if not user:
        return

    text = message.text or ""

    # --- ИИ-Модерация контактов до оплаты ---
    mod = await ai.moderate_message(text)
    if not mod.is_safe:
        await message.reply(
            f"🚫 **Сообщение заблокировано ИИ-Модератором!**\n⚠️ {mod.reason}",
            parse_mode=ParseMode.MARKDOWN,
        )
        if mod.cleaned_text and mod.cleaned_text.strip():
            text = mod.cleaned_text
        else:
            return

    menu_texts = {
        "📋 Իմ հայտերը / Мои заявки",
        "📂 Ուղղություններ / Направления",
        "📊 Պատմություն / История",
        "📷 Փակել գործարքը / Закрыть сделку",
        "📝 Նոր հայտ / Новая заявка",
    }
    if text in menu_texts:
        return

    if user.get("role") == "client":
        await _process_client_request(message, text, state)
    elif user.get("role") == "master":
        await _handle_master_chat_message(message, text, state)


async def _process_client_request(message: types.Message, text: str, state: FSMContext):
    """Полный цикл обработки заявки клиента через ИИ-диспетчера Groq."""
    uid = message.from_user.id
    user = db.get_user(uid)
    lang = (user or {}).get("lang", "hy")

    await bot.send_chat_action(uid, "typing")

    cats = db.get_active_categories()
    analysis = await ai.analyze_request(text, cats)

    if analysis.category_hy in ["Ошибка API", "Внутренняя ошибка парсинга"]:
        await message.reply(f"🤖 {analysis.summary}")
        return

    city = analysis.city or "Ереван"

    order_id = db.create_order(
        client_id=uid,
        category_id=analysis.category_id or 0,
        category_name_hy=analysis.category_hy,
        summary=analysis.summary,
        checklist=analysis.checklist,
        city=city,
        lang=lang,
    )

    matching = db.find_matching_masters(analysis.category_hy, city)
    sent_count = 0
    if matching:
        for master in matching:
            try:
                await bot.send_message(
                    chat_id=master["telegram_id"],
                    text=(
                        f"🔔 **ՆՈՐ ՊԱՏՎԵՐ / НОВЫЙ ЗАКАЗ!**\n"
                        f"📍 {city}\n"
                        f"📂 {analysis.category_hy}\n\n"
                        f"📋 {analysis.summary}"
                    ),
                    reply_markup=get_master_accept_keyboard(order_id, uid),
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent_count += 1
            except Exception:
                pass

    status = (
        f"🚀 Գտնվել են {sent_count} վարպետ / Найдено мастеров: **{sent_count}**"
        if sent_count > 0
        else f"📍 Վարպետներ չկան / Мастеров пока нет в регионе {city}"
    )

    await message.reply(
        f"🤖 **ԻԻ-Դիսպետչер / ИИ-Диспетчер:**\n\n"
        f"📂 Կատեգորիա / Категория: {analysis.category_hy}\n"
        f"{status}\n\n"
        f"📋 Чек-лист:\n" + "\n".join(f"🔹 {q}" for q in analysis.checklist),
        parse_mode=ParseMode.MARKDOWN,
    )
# ═══════════════════════════════════════════════════════════════
# 4. МАСТЕР ОТКЛИКАЕТСЯ → ТОРГИ
# ═══════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("master_accept_"))
async def master_accept(callback: types.CallbackQuery, state: FSMContext):
    """Мастер нажимает кнопку 'Откликнуться' на заявке."""
    parts = callback.data.split("_")
    order_id = int(parts[2])
    client_id = int(parts[3])

    await callback.message.edit_text(
        "✅ Դուք ընդունեցիք / Вы откликнулись!\n\n"
        "💬 Մուտքագրեք ձեր գինը (AMD) / Введите вашу цену (AMD):"
    )
    await state.set_state(BiddingStates.awaiting_master_price)
    await state.update_data(order_id=order_id, client_id=client_id)
    await callback.answer()


@router.callback_query(F.data == "master_skip")
async def master_skip(callback: types.CallbackQuery):
    """Мастер нажал пропустить заказ."""
    await callback.message.edit_text("⏭️ Բաց է թողնված / Пропущено")
    await callback.answer()


@router.message(BiddingStates.awaiting_master_price)
async def master_enter_price(message: types.Message, state: FSMContext):
    """Мастер вводит цену текстом — AI Groq извлекает сумму в AMD."""
    data = await state.get_data()
    order_id = data.get("order_id")
    client_id = data.get("client_id")

    bid = await ai.extract_bid(message.text)
    if not bid.is_price_proposal or bid.amount <= 0:
        await message.answer("⚠️ Մուտքագրեք գինը / Введите корректную цену (например: 25000)")
        return

    db.create_bid(order_id, message.from_user.id, bid.amount, message.text)

    order = db.get_order(order_id)
    cat_name = (order or {}).get("category_name_hy", "")
    user = db.get_user(client_id)
    lang = (user or {}).get("lang", "hy")

    await bot.send_message(
        client_id,
        f"🔧 **{cat_name}**\n\n"
        f"Մասնագետն առաջարկում է / Мастер предлагает: **{bid.amount:,.0f} ֏**\n\n"
        f"Что делаем?",
        reply_markup=get_bid_actions_keyboard(order_id, message.from_user.id, bid.amount),
        parse_mode=ParseMode.MARKDOWN,
    )

    await state.clear()
    await message.answer(
        f"✅ Ձեր առաջարկը ուղարկված է / Ваша ставка отправлена: **{bid.amount:,.0f} ֏**",
        parse_mode=ParseMode.MARKDOWN,
    )


# ═══════════════════════════════════════════════════════════════
# 5. КЛИЕНТ: ПРИНЯТЬ / ПОТОРГОВАТЬСЯ / ОТКЛОНИТЬ
# ═══════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("accept_bid_"))
async def client_accept_bid(callback: types.CallbackQuery):
    """Клиент принимает цену мастера — формируем чек бронирования."""
    parts = callback.data.split("_")
    order_id = int(parts[2])
    master_id = int(parts[3])

    db.accept_bid(order_id, master_id)

    bids = db.get_bids(order_id)
    accepted = next((b for b in bids if b["master_id"] == master_id), None)
    price = float(accepted["amount"]) if accepted else 0

    order = db.get_order(order_id)
    cat_id = (order or {}).get("category_id", 0)
    client_id = callback.from_user.id
    user = db.get_user(client_id)
    lang = (user or {}).get("lang", "hy")

    receipt = billing_mgr.format_booking_receipt(cat_id, price, lang)
    db.update_order_status(order_id, "awaiting_payment")
    db.update_order_price(order_id, price)

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💳 Ամրագրել / Забронировать",
        web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/booking.html?order_id={order_id}"),
    )
    builder.adjust(1)

    await callback.message.edit_text(
        f"{receipt}\n\nНажмите для оплаты комиссии:",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.MARKDOWN,
    )

    await bot.send_message(
        master_id,
        f"✅ Դուք ընտրված եք / Вас выбрали!\n"
        f"📦 Заказ #{order_id}\n"
        f"💰 Сумма: {price:,.0f} ֏\n\n"
        f"Ожидайте оплату комиссии от клиента...",
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("counter_bid_"))
async def client_counter_bid(callback: types.CallbackQuery, state: FSMContext):
    """Клиент нажимает 'Поторговаться'."""
    parts = callback.data.split("_")
    order_id = int(parts[2])
    master_id = int(parts[3])
    original_amount = float(parts[4])

    await callback.message.edit_text(
        f"🔄 Գրեք ձեր առաջարկը / Напишите вашу встречную цену:\n"
        f"(Մասնագետի առաջարկը / Ставка мастера: {original_amount:,.0f} ֏)"
    )
    await state.set_state(BiddingStates.awaiting_client_counter)
    await state.update_data(order_id=order_id, master_id=master_id, original_amount=original_amount)
    await callback.answer()


@router.message(BiddingStates.awaiting_client_counter)
async def process_client_counter(message: types.Message, state: FSMContext):
    """Клиент вводит встречную цену — генерируем вежливый текст с торгом."""
    data = await state.get_data()
    order_id = data.get("order_id")
    master_id = data.get("master_id")
    lang = (db.get_user(message.from_user.id) or {}).get("lang", "hy")

    counter_text = await ai.generate_counter_offer(
        data.get("original_amount", 0), message.text, lang
    )

    bid = await ai.extract_bid(message.text)
    if bid.is_price_proposal and bid.amount > 0:
        db.counter_bid(order_id, master_id, bid.amount)

    await bot.send_message(
        master_id,
        f"🔄 **Встречное предложение от клиента:**\n\n{counter_text}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.clear()
    await message.reply("✅ Ձեր առաջարկը ուղարկված է / Встречное предложение отправлено мастеру.")


@router.callback_query(F.data.startswith("reject_bid_"))
async def client_reject_bid(callback: types.CallbackQuery):
    """Клиент отклоняет предложение мастера."""
    parts = callback.data.split("_")
    master_id = int(parts[3])
    await callback.message.edit_text("❌ Առաջարկը մերժված է / Ставка отклонена.")
    await bot.send_message(master_id, "❌ Լիենտը մերժել է / Клиент отклонил вашу ставку.")
    await callback.answer()
# ═══════════════════════════════════════════════════════════════
# 6. ОПЛАТА ЧЕРЕЗ IDRAM → ВАУЧЕР И QR-КОДЫ
# ═══════════════════════════════════════════════════════════════

async def handle_idram_callback(request):
    """Webhook от Idram после успешной оплаты комиссии клиентом."""
    try:
        data = await request.json()
        if data.get("status") == "payment_success":
            user_id = int(data.get("user_id", 0))
            order_id = int(data.get("order_id", 0))
            idram_txn = data.get("txn_id", "")

            if not (user_id and order_id):
                return web.Response(text="Missing params", status=400)

            order = db.get_order(order_id)
            if not order:
                return web.Response(text="Order not found", status=404)

            # Idram/webhook can be delivered more than once. Once an order is
            # booked, do not create a second deal or send a second QR.
            if order.get("status") == "booked":
                return web.Response(text="OK", status=200)

            deal_id = BillingManager.generate_deal_id()
            secure_code = BillingManager.generate_secure_code()

            cat_id = order.get("category_id", 0)
            price = float(order.get("final_price", 0) or 0)
            calc = billing_mgr.calculate_commission(cat_id, price) if cat_id and price else {}
            commission = calc.get("commission", 0)
            master_payout = calc.get("master_payout", 0)

            bids = db.get_bids(order_id)
            master = next((b for b in bids if b["status"] == "accepted"), None)
            master_id = master["master_id"] if master else None

            db.create_deal(
                deal_id=deal_id, order_id=order_id,
                client_id=user_id, master_id=master_id or 0,
                secure_code=secure_code, total_price=price, 
                commission=commission, master_payout=master_payout
            )
            db.update_deal_status(deal_id, "active", idram_txn)
            db.update_order_status(order_id, "booked")

            bot_info = await bot.get_me()
            qr_file = billing_mgr.create_deal_qr(deal_id, secure_code, bot_info.username)

            await bot.send_photo(
                user_id, photo=qr_file,
                caption=(
                    f"🎉 **Սդելկան ակտիվ / Сделка #{deal_id} забронирована!**\n\n"
                    f"🔑 **Կոդ / Код:** `{secure_code}`\n\n"
                    f"📱 Покажите QR-код мастеру после завершения работы."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )

            client_user = db.get_user(user_id)
            if master_id:
                master_user = db.get_user(master_id)
                if master_user:
                    await bot.send_message(
                        master_id,
                        f"✅ **Оплата получена! Сделка #{deal_id} активна.**\n\n"
                        f"👤 Клиент: @{client_user.get('username', 'N/A')}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await bot.send_message(
                        user_id,
                        f"👨‍🔧 **Мастер:** @{master_user.get('username', 'N/A')}",
                        parse_mode=ParseMode.MARKDOWN,
                    )

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Ошибка Idram callback: {e}")
        return web.Response(text="Error", status=500)


# ═══════════════════════════════════════════════════════════════
# 7. ЗАКРЫТИЕ СДЕЛКИ ВРУЧНУЮ
# ═══════════════════════════════════════════════════════════════

@router.message(F.text == "📷 Փակել գործարքը / Закрыть сделку")
async def close_deal_menu(message: types.Message):
    """Меню ручного закрытия безопасной сделки."""
    await message.answer(
        "🔒 Փակել գործարքը / Закрыть сделку:\n\n"
        "Մուտքագրեք 6-նիշ կոդը կամ օգտագործեք QR-ն:\n"
        "Введите 6-значный код сделки или используйте QR:",
        reply_markup=get_close_deal_keyboard(),
    )


@router.callback_query(F.data == "enter_code")
async def enter_code_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔢 Մուտքագրեք գործարքի ID-ն / Введите ID сделки:")
    await state.set_state(CodeEntryStates.entering_deal_id)
    await callback.answer()


@router.message(CodeEntryStates.entering_deal_id)
async def enter_deal_id(message: types.Message, state: FSMContext):
    deal_id = message.text.strip()
    await state.update_data(deal_id=deal_id)
    await state.set_state(CodeEntryStates.entering_secure_code)
    await message.answer("🔑 Մուտքագրեք 6-նիշ կոդը / Введите 6-значный код безопасности:")


@router.message(CodeEntryStates.entering_secure_code)
async def enter_secure_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    deal_id = data.get("deal_id", "")
    master_id = message.from_user.id

    result = db.verify_and_close_deal(deal_id, code, master_id)
    if result == "ok":
        deal = db.get_deal(deal_id)
        await message.answer(
            f"✅ **Սդելկա #{deal_id} փակված է / Сделка закрыта!**\n\n"
            f"🎉 Отличная работа!",
            parse_mode=ParseMode.MARKDOWN,
        )
        if deal and deal.get("client_id"):
            await bot.send_message(
                deal["client_id"],
                f"✅ **Сделка #{deal_id} закрыта!**\n\n"
                f"Оцените работу мастера:",
                reply_markup=get_rating_keyboard(deal_id, master_id),
                parse_mode=ParseMode.MARKDOWN,
            )
    elif result == "already_closed":
        await message.answer("ℹ️ Эта сделка уже закрыта.")
    else:
        await message.answer("❌ Սխալ կոդ / Неверный код. Попробуйте снова.")

    await state.clear()


# ═══════════════════════════════════════════════════════════════
# 8. ОЦЕНКА МАСТЕРА И АНОНИМНЫЙ ЧАТ
# ═══════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("rate_"))
async def rate_master(callback: types.CallbackQuery):
    """Сбор отзывов и пересчет рейтинга."""
    parts = callback.data.split("_")
    deal_id = parts[1]
    master_id = int(parts[2])
    rating = int(parts[3])

    db.create_review(deal_id, callback.from_user.id, master_id, rating)
    await callback.message.edit_text(f"⭐ Շնորհակալություն / Спасибо за оценку: {'⭐' * rating}")
    await callback.answer()


async def _handle_master_chat_message(message: types.Message, text: str, state: FSMContext):
    """Анонимный чат: трансляция с модерацией."""
    uid = message.from_user.id
    active_chat = db.get_active_chat_for_master(uid)
    if not active_chat:
        await message.answer("⚠️ У вас нет активных чатов.")
        return

    order_id = active_chat["order_id"]
    client_id = active_chat["client_id"]

    mod = await ai.moderate_message(text)
    if not mod.is_safe:
        await message.reply(f"🚫 Сообщение заблокировано: {mod.reason}")
        db.save_chat_message(order_id, uid, client_id, text, True, mod.reason)
        return

    db.save_chat_message(order_id, uid, client_id, mod.cleaned_text)
    await bot.send_message(client_id, f"💬 Мастер: {mod.cleaned_text}")


@router.message(F.text == "📂 Ուղղություններ / Направления")
async def open_master_cabinet(message: types.Message):
    """Вход в кабинет Web App мастера."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📂 Բացել սենյակը / Открыть кабинет", web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/master_cabinet.html"))
    await message.answer("Нажмите для входа в кабинет:", reply_markup=builder.as_markup())


# ═══════════════════════════════════════════════════════════════
# 9. АРБИТРАЖ / СПОРЫ И СИМУЛЯЦИЯ
# ═══════════════════════════════════════════════════════════════

@router.message(Command("dispute"))
async def cmd_dispute(message: types.Message):
    """Команда открытия спора."""
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚖️ Формат: /dispute <deal_id> <причина>")
        return
        
    args = parts[1].split(maxsplit=1)
    deal_id = args[0]
    reason = args[1] if len(args) > 1 else "Без причины"

    deal = db.get_deal(deal_id)
    if not deal:
        await message.answer("❌ Сделка не найдена.")
        return
    if deal["status"] == "closed":
        await message.answer("ℹ️ Сделка уже закрыта.")
        return

    dispute_id = db.create_dispute(deal_id, uid, reason)
    await message.answer(f"⚖️ **Спор #{dispute_id} открыт!**", parse_mode=ParseMode.MARKDOWN)
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"⚖️ **Новый спор!**\nСделка #{deal_id}, причина: {reason}", parse_mode=ParseMode.MARKDOWN)


@router.message(Command("pay"))
async def cmd_simulate_pay(message: types.Message):
    """Тестовая команда симуляции Idram оплаты."""
    deal_id = BillingManager.generate_deal_id()
    secure_code = BillingManager.generate_secure_code()
    bot_info = await bot.get_me()
    qr_file = billing_mgr.create_deal_qr(deal_id, secure_code, bot_info.username)
    await message.answer_photo(photo=qr_file, caption=f"💳 **Симуляция оплаты**\n\n🔑 Сделка #{deal_id}\n🔒 Код: `{secure_code}`", parse_mode=ParseMode.MARKDOWN)
# ═══════════════════════════════════════════════════════════════
# 10. API ЭНДПОИНТЫ (REST API ДЛЯ WEB APPS И АДМИН-ПАНЕЛИ)
# ═══════════════════════════════════════════════════════════════

async def api_order_detail(request):
    """GET /api/order/{id} — данные заказа для Web App бронирования."""
    order_id = int(request.match_info["id"])
    order = db.get_order(order_id)
    if not order:
        return web.json_response({"error": "not found"}, status=404)
    bids = db.get_bids(order_id)
    accepted = next((b for b in bids if b["status"] == "accepted"), None)
    cat_id = order.get("category_id", 0)
    price = float(order.get("final_price", 0) or 0)
    calc = billing_mgr.calculate_commission(cat_id, price) if cat_id and price else {}
    master_user = db.get_user(accepted["master_id"]) if accepted else None
    return web.json_response({
        "order_id": order_id, "category_name": order.get("category_name_hy", ""),
        "master_name": master_user.get("username", "") if master_user else "—",
        "city": order.get("city", ""), "master_price": price,
        "commission": calc.get("commission", 0), "master_payout": calc.get("master_payout", 0),
        "total": calc.get("total", 0),
    })

async def api_idram_init(request):
    data = await request.json()
    return web.json_response({"redirect_url": f"{WEBAPP_BASE_URL}/api/idram/success", "order_id": data.get("order_id")})

async def api_idram_success(request):
    return await handle_idram_callback(request)

async def api_idram_fail(request):
    return web.Response(text="Payment failed", status=400)

async def api_master_orders(request):
    uid = int(request.match_info["id"])
    return web.json_response(db.get_master_feed(uid))

async def api_master_categories(request):
    uid = int(request.match_info["id"])
    return web.json_response(db.get_master_categories(uid))

async def api_master_respond(request):
    """WebApp отклик мастера на legacy order.

    Если передана цена, создаём настоящую bid-запись; без цены только
    уведомляем клиента. Это сохраняет совместимость со старым flow.
    """
    uid = int(request.match_info["id"])
    data = await request.json()
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_order_id"}, status=400)

    order = db.get_order(order_id)
    if not order:
        return web.json_response({"error": "order_not_found"}, status=404)
    if order.get("client_id") is None:
        return web.json_response({"error": "client_not_found"}, status=400)

    # Do not let a different Telegram user submit a bid for this order.
    try:
        matching = db.find_matching_masters(order.get("category_name_hy") or "", order.get("city") or "")
        if matching and not any(int(m.get("telegram_id")) == uid for m in matching):
            return web.json_response({"error": "master_not_authorized"}, status=403)
    except Exception:
        # New partner cabinet may not use legacy matching; ownership is then
        # checked by the new cabinet API. Keep this endpoint backward compatible.
        pass

    action = str(data.get("action") or "accept").lower()
    if action in {"skip", "reject", "decline"}:
        return web.json_response({"ok": True, "status": "skipped"})

    raw_amount = data.get("amount", data.get("price"))
    if raw_amount not in (None, ""):
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_amount"}, status=400)
        if amount <= 0:
            return web.json_response({"error": "amount_must_be_positive"}, status=400)
        bid_id = db.create_bid(order_id, uid, amount, str(data.get("message") or ""))
        await bot.send_message(
            order["client_id"],
            f"📥 Մասնագետը արձագանքեց #{order_id}-ին։ / Мастер откликнулся на заявку #{order_id}.\n"
            f"💰 Առաջարկ / Предложение: **{amount:,.0f} ֏**",
            parse_mode=ParseMode.MARKDOWN,
        )
        return web.json_response({"ok": True, "bid_id": bid_id, "amount": amount})

    await bot.send_message(
        order["client_id"],
        f"📥 Մասնագետը արձագանքեց #{order_id} հայտին! / Мастер откликнулся!"
    )
    return web.json_response({"ok": True, "status": "responded"})

async def api_master_toggle_category(request):
    uid = int(request.match_info["id"])
    data = await request.json()
    db.toggle_master_category(uid, data.get("category_id"), data.get("is_active", True))
    return web.json_response({"ok": True})

async def api_master_history(request):
    uid = int(request.match_info["id"])
    try:
        data = db.get_master_history(uid) or {}
    except Exception:
        logger.exception("API master history failed for %s", uid)
        data = {"completed_count": 0, "total_earned": 0, "total_commission": 0, "avg_rating": None, "deals": []}
    return web.json_response(data)
# --- API ДЛЯ АДМИН-ПАНЕЛИ (СТАТИСТИКА, МОДЕРАЦИЯ И CRUD КАТАЛОГА) ---

async def api_admin_stats(request):
    """GET /api/admin/stats — Безопасная отдача общей операционной и финансовой статистики."""
    try:
        stats = db.get_admin_stats() or {}
        return web.json_response({
            "total_users": int(stats.get("total_users") or 0),
            "total_masters": int(stats.get("total_masters") or 0),
            "verified_masters": int(stats.get("verified_masters") or 0),
            "total_orders": int(stats.get("total_orders") or 0),
            "completed_orders": int(stats.get("completed_orders") or 0),
            "total_commission": float(stats.get("total_commission") or 0),
            "open_disputes": int(stats.get("open_disputes") or 0)
        })
    except Exception as e:
        return web.json_response({"total_users":0,"total_masters":0,"verified_masters":0,"total_orders":0,"completed_orders":0,"total_commission":0,"open_disputes":0})

async def api_admin_users(request):
    """GET /api/admin/users — Список всех пользователей с приведением типов для фронтенда."""
    try:
        users = db.get_all_users()
        formatted = []
        for u in users:
            created_str = str(u.get("created_at", ""))[:19] if u.get("created_at") else ""
            formatted.append({
                "telegram_id": u.get("telegram_id"), "username": u.get("username", "N/A"),
                "full_name": u.get("full_name", ""), "role": u.get("role"), "lang": u.get("lang", "hy"),
                "city": u.get("city"), "phone": u.get("phone"), "is_verified": bool(u.get("is_verified", False)),
                "is_frozen": bool(u.get("is_frozen", False)), "balance": float(u.get("balance", 0) or 0), "created_at": created_str
            })
        return web.json_response(formatted)
    except Exception as e:
        return web.json_response([], status=500)

async def api_admin_categories(request):
    """GET /api/admin/categories — Древовидная отдача структуры каталога с именами родителей."""
    try:
        categories = db.get_all_categories()
        formatted = []
        for cat in categories:
            formatted.append({
                "id": cat.get("id"), "master_category_id": cat.get("master_category_id"),
                "name_hy": cat.get("name_hy", ""), "name_ru": cat.get("name_ru", ""),
                "name_en": cat.get("name_en", ""), "commission_type": cat.get("commission_type", "on_top"),
                "commission_value": float(cat.get("commission_value", 10) or 0), "is_active": cat.get("is_active", True),
                "master_name_ru": cat.get("master_name_ru", ""), "master_name_am": cat.get("master_name_am", "")
            })
        return web.json_response(formatted)
    except Exception as e:
        return web.json_response([], status=500)

async def api_admin_category_update(request):
    """POST /api/admin/category/{id} — Изменение параметров (комиссии, активности) подкатегории."""
    cat_id = int(request.match_info["id"])
    data = await request.json()
    db.update_category(cat_id, **data)
    return web.json_response({"ok": True})

async def api_admin_master_category_create(request):
    """POST /api/admin/master_category — Создание новой родительской сферы."""
    data = await request.json()
    new_id = db.create_master_category(data['name_ru'], data['name_am'], data['slug'])
    return web.json_response({"ok": True, "id": new_id})

async def api_admin_master_category_update(request):
    """POST /api/admin/master_category/{id} — Редактирование существующей родительской сферы."""
    mcat_id = int(request.match_info["id"])
    data = await request.json()
    db.update_master_category(mcat_id, **data)
    return web.json_response({"ok": True})

async def api_admin_master_category_delete(request):
    """DELETE /api/admin/master_category/{id} — Удаление родительской сферы (каскадное)."""
    mcat_id = int(request.match_info["id"])
    db.delete_master_category(mcat_id)
    return web.json_response({"ok": True})

async def api_admin_subcategory_create(request):
    """POST /api/admin/subcategory — Создание новой подкатегории услуг."""
    data = await request.json()
    new_id = db.create_subcategory(
        master_category_id=int(data['master_category_id']), name_ru=data['name_ru'],
        name_am=data['name_am'], slug=data['slug'], commission_type=data.get('commission_type', 'on_top'),
        commission_value=float(data.get('commission_value', 10.0))
    )
    return web.json_response({"ok": True, "id": new_id})

async def api_admin_subcategory_delete(request):
    """DELETE /api/admin/subcategory/{id} — Удаление конкретной услуги мастера."""
    cat_id = int(request.match_info["id"])
    db.delete_subcategory(cat_id)
    return web.json_response({"ok": True})
async def api_admin_orders(request):
    """GET /api/admin/orders — Получить список всех заказов платформы."""
    orders = db.get_all_orders()
    return web.json_response(orders)

async def api_admin_disputes(request):
    """GET /api/admin/disputes — Получить список всех открытых споров."""
    disputes = db.get_open_disputes()
    return web.json_response(disputes)

async def api_admin_user_action(request):
    """POST /api/admin/user/{id}/{action} — Действия модератора над пользователем."""
    uid = int(request.match_info["id"])
    action = request.match_info["action"]
    if action == "verify":
        db.update_user_field(uid, "is_verified", True)
    elif action == "freeze":
        current = db.get_user(uid)
        db.update_user_field(uid, "is_frozen", not (current or {}).get("is_frozen", False))
    elif action == "delete":
        db.delete_user(uid)
    return web.json_response({"ok": True})

async def api_admin_dispute_resolve(request):
    """POST /api/admin/dispute/{id}/resolve — Вынесение вердикта арбитража."""
    dispute_id = int(request.match_info["id"])
    data = await request.json()
    db.resolve_dispute(dispute_id, "Resolved by admin", data.get("refund", False))
    return web.json_response({"ok": True})


# ═══════════════════════════════════════════════════════════════
# ЗАПУСК HTTP-СЕРВЕРА И TG БОТА (ОБЩИЙ ЦИКЛ)
# ═══════════════════════════════════════════════════════════════

async def main():
    logger.info("🚀 Запуск ИИ-Маркетплейса Армении...")

    app = web.Application(
        middlewares=[telegram_webapp_auth_middleware]
    )

    # Health / public endpoints
    app.router.add_get("/health", health)
    app.router.add_get("/api/order/{id}", api_order_detail)
    app.router.add_post("/api/idram/init", api_idram_init)
    app.router.add_post("/api/idram/callback", handle_idram_callback)
    app.router.add_post("/api/idram/success", api_idram_success)
    app.router.add_post("/api/idram/fail", api_idram_fail)

    # Partner registration — доступно до одобрения партнёра.
    app.router.add_get(
        "/api/master/{id}/registration-status",
        api_partner_registration_status,
    )
    app.router.add_post(
        "/api/master/{id}/register",
        api_partner_register,
    )

    # Legacy master endpoints.
    app.router.add_get("/api/master/{id}/orders", api_master_orders)
    app.router.add_get("/api/master/{id}/categories", api_master_categories)
    app.router.add_post("/api/master/{id}/respond", api_master_respond)
    app.router.add_post("/api/master/{id}/toggle_category", api_master_toggle_category)
    app.router.add_get("/api/master/{id}/history", api_master_history)

    # Admin base endpoints.
    app.router.add_get("/api/admin/stats", api_admin_stats)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_get("/api/admin/categories", api_admin_categories)
    app.router.add_post("/api/admin/category/{id}", api_admin_category_update)
    app.router.add_get("/api/admin/orders", api_admin_orders)
    app.router.add_get("/api/admin/disputes", api_admin_disputes)
    app.router.add_post("/api/admin/user/{id}/{action}", api_admin_user_action)
    app.router.add_post("/api/admin/dispute/{id}/resolve", api_admin_dispute_resolve)

    # Admin catalogue CRUD.
    app.router.add_post("/api/admin/master_category", api_admin_master_category_create)
    app.router.add_post("/api/admin/master_category/{id}", api_admin_master_category_update)
    app.router.add_delete("/api/admin/master_category/{id}", api_admin_master_category_delete)
    app.router.add_post("/api/admin/subcategory", api_admin_subcategory_create)
    app.router.add_delete("/api/admin/subcategory/{id}", api_admin_subcategory_delete)

    # Stage 3 — partner verification / admin moderation.
    register_stage3_routes(
        app,
        bot_token=BOT_TOKEN,
        admin_id=ADMIN_ID,
    )
    logger.info("✅ Stage 3 verification routes registered")

    # New partner cabinet.
    if register_master_cabinet_routes is not None:
        try:
            register_master_cabinet_routes(app, db, bot=bot)
            logger.info("✅ Partner cabinet API registered")
        except Exception:
            logger.exception("Не удалось зарегистрировать Partner cabinet API")

    # Welcome WebApp role selection. This endpoint validates Telegram initData.
    app.router.add_post("/api/webapp/role", api_webapp_set_role)
    app.router.add_post("/api/webapp/partner/start", api_webapp_partner_start)

    # Public home page — explicitly serve index.html.
    app.router.add_get("/", serve_index)

    # Static Web App assets.
    app.router.add_static(
        "/",
        path=str(WEB_APPS_DIR),
        name="web_apps",
    )

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("🌐 HTTP-сервер запущен на порту %s", port)

    # Telegram bottom menu: one-tap entry into the beautiful Welcome WebApp.
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Присоединиться",
                web_app=WebAppInfo(url=webapp_url("welcome.html")),
            )
        )
        logger.info("✅ Telegram bottom menu button configured")
    except Exception:
        logger.exception("Не удалось настроить Telegram bottom menu button")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
