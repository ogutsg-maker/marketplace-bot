"""
Глобальная конфигурация проекта.
Все настройки загружаются из .env с fallback-значениями.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# --- Загрузка .env ---
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Telegram ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")

ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

# --- AI Providers ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# --- Database ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в .env")

# --- Idram Payment Gateway ---
IDRAM_MERCHANT_ID = os.getenv("IDRAM_MERCHANT_ID", "test")
IDRAM_SECRET_KEY = os.getenv("IDRAM_SECRET_KEY", "test")
IDRAM_SUCCESS_URL = os.getenv("IDRAM_SUCCESS_URL", "")
IDRAM_FAIL_URL = os.getenv("IDRAM_FAIL_URL", "")

# --- Web App URLs (обновляются при деплое) ---
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "https://your-domain.com")

# --- Supported languages ---
SUPPORTED_LANGS = {"hy", "ru", "en"}
DEFAULT_LANG = "hy"

# --- City synonyms for smart matching ---
CITY_SYNONYMS = {
    "ереван": ["ереван", "երևան", "yerevan", "erevan"],
    "гюмри": ["гюмри", "գյումրի", "gyumri", "gumri"],
    "ванадзор": ["ванадзор", "վանաձոր", "vanadzor"],
    "дилижан": ["дилижан", "դիլիջան", "dilijan"],
    "севан": ["севан", "սևան", "sevan"],
    "тбилиси": ["тбилиси", "թբիլիսի", "tbilisi"],
}

# --- Commission types ---
COMMISSION_INSIDE = "inside"     # внутри цены (% от тарифа мастера)
COMMISSION_ON_TOP = "on_top"     # сверх цены (% добавляется к цене)
COMMISSION_FIXED = "fixed"       # фиксированная сумма (AMD)
COMMISSION_TYPES = [COMMISSION_INSIDE, COMMISSION_ON_TOP, COMMISSION_FIXED]