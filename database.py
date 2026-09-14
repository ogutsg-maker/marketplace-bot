"""DatabaseManager — единый слой работы с PostgreSQL (Supabase / любой PostgreSQL)."""
import os
import json
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier
from config import DATABASE_URL, CITY_SYNONYMS

logger = logging.getLogger(__name__)

def _connection_kwargs() -> dict:
    """Возвращает параметры подключения с автоподстройкой под Supabase."""
    url = DATABASE_URL
    kwargs = {"row_factory": dict_row}
    if "supabase" in url or "supabase.co" in url:
        kwargs["sslmode"] = "require"
    elif "sslmode" not in url:
        kwargs["sslmode"] = "prefer"
    return kwargs

def _connect(url: str = None, row_factory=None):
    """Удобная обёртка: автоматически подставляет sslmode и фабрику строк."""
    url = url or DATABASE_URL
    kwargs = {"row_factory": row_factory or dict_row}
    if "supabase" in url or "supabase.co" in url:
        kwargs["sslmode"] = "require"
    elif "sslmode" not in url:
        kwargs["sslmode"] = "prefer"
    return psycopg.connect(url, **kwargs)
class DatabaseManager:
    def __init__(self):
        self.db_url = DATABASE_URL
        self.init_db()

    def init_db(self):
        """Создание таблиц маркетплейса при первом запуске."""
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    # 1. Таблица пользователей
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS users (
                            telegram_id  BIGINT PRIMARY KEY,
                            username     TEXT,
                            full_name    TEXT,
                            role         TEXT DEFAULT NULL CHECK (role IN ('client','master',NULL)),
                            lang         TEXT DEFAULT 'hy',
                            city         TEXT DEFAULT NULL,
                            phone        TEXT DEFAULT NULL,
                            passport_photo TEXT DEFAULT NULL,
                            is_verified BOOLEAN DEFAULT FALSE,
                            is_frozen    BOOLEAN DEFAULT FALSE,
                            balance      NUMERIC DEFAULT 0,
                            rating_avg   NUMERIC DEFAULT NULL,
                            rating_count INT DEFAULT 0,
                            created_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 2. Главные родительские категории (сферы)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS master_categories (
                            id           SERIAL PRIMARY KEY,
                            name_am      TEXT NOT NULL,
                            name_ru      TEXT NOT NULL,
                            slug         TEXT NOT NULL UNIQUE,
                            created_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 3. Подкатегории услуг (конкретные услуги)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS categories (
                            id           SERIAL PRIMARY KEY,
                            master_category_id INT REFERENCES master_categories(id) ON DELETE CASCADE,
                            name_am      TEXT NOT NULL,
                            name_ru      TEXT NOT NULL,
                            slug         TEXT NOT NULL UNIQUE,
                            is_active    BOOLEAN DEFAULT TRUE,
                            commission_type  TEXT NOT NULL DEFAULT 'on_top'
                                CHECK (commission_type IN ('inside','on_top','fixed')),
                            commission_value NUMERIC NOT NULL DEFAULT 10,
                            created_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 4. Связь мастер ↔ подкатегории (M:N)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS master_skills (
                            id          SERIAL PRIMARY KEY,
                            user_id     BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                            category_id INT REFERENCES categories(id) ON DELETE CASCADE,
                            description TEXT DEFAULT '',
                            is_active   BOOLEAN DEFAULT TRUE,
                            created_at  TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE(user_id, category_id)
                        )
                    ''')
                    # 5. Заказы
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS orders (
                            id           SERIAL PRIMARY KEY,
                            client_id    BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                            category_id  INT REFERENCES categories(id),
                            category_name_hy TEXT,
                            summary      TEXT,
                            checklist_json TEXT,
                            city         TEXT,
                            lang         TEXT DEFAULT 'hy',
                            status       TEXT NOT NULL DEFAULT 'collecting'
                                CHECK (status IN (
                                    'collecting','bidding','awaiting_payment',
                                    'booked','completed','cancelled','disputed'
                                )),
                            final_price  NUMERIC DEFAULT NULL,
                            commission_amount NUMERIC DEFAULT NULL,
                            created_at   TIMESTAMPTZ DEFAULT NOW(),
                            updated_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 6. Сделки
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS deals (
                            deal_id      TEXT PRIMARY KEY,
                            order_id     INT REFERENCES orders(id) ON DELETE CASCADE,
                            client_id    BIGINT REFERENCES users(telegram_id),
                            master_id    BIGINT REFERENCES users(telegram_id),
                            secure_code  TEXT NOT NULL,
                            total_price  NUMERIC DEFAULT 0,
                            commission   NUMERIC DEFAULT 0,
                            master_payout NUMERIC DEFAULT 0,
                            status       TEXT NOT NULL DEFAULT 'wait_payment'
                                CHECK (status IN (
                                    'wait_payment','active','closed','disputed','refunded'
                                )),
                            idram_txn_id TEXT DEFAULT NULL,
                            created_at   TIMESTAMPTZ DEFAULT NOW(),
                            closed_at    TIMESTAMPTZ DEFAULT NULL
                        )
                    ''')
                    # 7. Анонимный чат
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS chat_messages (
                            id          SERIAL PRIMARY KEY,
                            order_id    INT REFERENCES orders(id) ON DELETE CASCADE,
                            sender_id   BIGINT NOT NULL,
                            receiver_id BIGINT NOT NULL,
                            text        TEXT,
                            is_blocked  BOOLEAN DEFAULT FALSE,
                            block_reason TEXT DEFAULT NULL,
                            created_at  TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 8. Торги (ставки мастеров)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS bids (
                            id          SERIAL PRIMARY KEY,
                            order_id    INT REFERENCES orders(id) ON DELETE CASCADE,
                            master_id   BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                            amount      NUMERIC NOT NULL,
                            message     TEXT DEFAULT '',
                            status      TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','accepted','rejected','counter')),  
                            created_at  TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE(order_id, master_id)
                        )
                    ''')
                    # 9. Отзывы
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS reviews (
                            id          SERIAL PRIMARY KEY,
                            deal_id     TEXT REFERENCES deals(deal_id),
                            client_id   BIGINT REFERENCES users(telegram_id),
                            master_id   BIGINT REFERENCES users(telegram_id),
                            rating      INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
                            comment     TEXT DEFAULT '',
                            created_at  TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 10. Арбитраж / споры
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS disputes (
                            id          SERIAL PRIMARY KEY,
                            deal_id     TEXT REFERENCES deals(deal_id),
                            initiated_by BIGINT REFERENCES users(telegram_id),
                            reason      TEXT NOT NULL,
                            status      TEXT NOT NULL DEFAULT 'open'
                                CHECK (status IN ('open','resolved','refunded')),
                            resolution TEXT DEFAULT NULL,
                            created_at  TIMESTAMPTZ DEFAULT NOW(),
                            resolved_at TIMESTAMPTZ DEFAULT NULL
                        )
                    ''')
                    conn.commit()
            logger.info("✅ База данных успешно инициализирована.")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            raise
    # ------------------------------------------------------------------
    # ПОЛЬЗОВАТЕЛИ
    # ------------------------------------------------------------------
    def register_user(self, telegram_id: int, username: str, full_name: str = None):
        """Регистрация нового пользователя или обновление юзернейма."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO users (telegram_id, username, full_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        full_name = COALESCE(EXCLUDED.full_name, users.full_name)
                ''', (telegram_id, username, full_name))
                conn.commit()

    def get_user(self, telegram_id: int) -> dict | None:
        """Получение профиля пользователя по его Telegram ID."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
                return cur.fetchone()

    def update_user_field(self, telegram_id: int, field: str, value):
        """Обновление любого выбранного поля пользователя в базе (безопасный динамический SQL)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    SQL("UPDATE users SET {} = %s WHERE telegram_id = %s").format(Identifier(field)),
                    (value, telegram_id)
                )
                conn.commit()

    def get_masters_by_city(self, city: str) -> list:
        """Получение списка активных верифицированных мастеров в конкретном городе с автоподбором синонимов."""
        with _connect() as conn:
            with conn.cursor() as cur:
                city_lower = city.strip().lower()
                allowed = CITY_SYNONYMS.get(city_lower, [city_lower])
                cur.execute('''
                    SELECT telegram_id, username, city, rating_avg
                    FROM users
                    WHERE role = 'master'
                      AND is_verified = TRUE
                      AND is_frozen = FALSE
                      AND TRIM(LOWER(city)) = ANY(%s)
                ''', (allowed,))
                return cur.fetchall()
    # ------------------------------------------------------------------
    # КАТЕГОРИИ УСЛУГ (ДВУХУРОВНЕВАЯ СТРУКТУРА)
    # ------------------------------------------------------------------
    def get_all_master_categories(self) -> list[dict]:
        """Возвращает список всех главных родительских категорий (сфер)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name_am, name_ru, slug FROM master_categories ORDER BY id")
                return cur.fetchall()

    def get_subcategories_by_master(self, master_category_id: int) -> list[dict]:
        """Возвращает список активных подкатегорий (услуг) для конкретной сферы."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, master_category_id, name_am, name_ru, slug, commission_type, commission_value 
                    FROM categories 
                    WHERE master_category_id = %s AND is_active = TRUE 
                    ORDER BY id
                ''', (master_category_id,))
                return cur.fetchall()

    def get_all_categories(self) -> list[dict]:
        """Возвращает все подкатегории вместе с именами их родительских сфер для админки."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT 
                        c.id, 
                        c.master_category_id, 
                        c.name_am as name_hy, 
                        c.name_ru, 
                        c.slug, 
                        c.commission_type, 
                        c.commission_value,
                        c.is_active,
                        m.name_ru as master_name_ru,
                        m.name_am as master_name_am
                    FROM categories c
                    JOIN master_categories m ON c.master_category_id = m.id
                    ORDER BY m.id, c.id
                ''')
                return cur.fetchall()

    def get_active_categories(self) -> list[dict]:
        """Возвращает только активные подкатегории (алиас для обратной совместимости с ai_dispatcher)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT 
                        c.id, 
                        c.master_category_id, 
                        c.name_am as name_hy, 
                        c.name_ru, 
                        c.slug, 
                        c.commission_type, 
                        c.commission_value,
                        c.is_active,
                        m.name_ru as master_name_ru,
                        m.name_am as master_name_am
                    FROM categories c
                    JOIN master_categories m ON c.master_category_id = m.id
                    WHERE c.is_active = TRUE 
                    ORDER BY m.id, c.id
                ''')
                return cur.fetchall()

    def get_category_by_name(self, name_to_find: str) -> dict | None:
        """Ищет подкатегорию по названию или по slug."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, master_category_id, name_am as name_hy, name_ru, slug, commission_type, commission_value 
                    FROM categories 
                    WHERE name_am = %s OR name_ru = %s OR slug = %s
                ''', (name_to_find, name_to_find, name_to_find))
                return cur.fetchone()

    def update_category(self, cat_id: int, **kwargs):
        """Обновление полей подкатегории (комиссия, активность и т.д.)."""
        if not kwargs:
            return
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f"{k} = %s")
            vals.append(v)
        vals.append(cat_id)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE categories SET {', '.join(sets)} WHERE id = %s",
                    vals,
                )
                conn.commit()

    def update_category_settings(self, cat_id: int, **kwargs):
        """Обновляет настройки подкатегории в categories + category_settings."""
        allowed = {
            "is_active", "commission_type", "commission_value",
            "bank_commission_type", "bank_commission_value",
            "cancellation_policy", "premium_contact_enabled",
            "premium_contact_fee", "premium_disclosure_scope",
            "contact_reveal_after_booking",
        }
        data = {k: v for k, v in kwargs.items() if k in allowed}
        if not data:
            return
        with _connect() as conn:
            with conn.cursor() as cur:
                # Основные параметры подкатегории.
                cat_fields = {k: data[k] for k in ("is_active", "commission_type", "commission_value") if k in data}
                if cat_fields:
                    sets = [f"{k} = %s" for k in cat_fields]
                    vals = list(cat_fields.values()) + [cat_id]
                    cur.execute(f"UPDATE categories SET {', '.join(sets)} WHERE id = %s", vals)

                # Дополнительные коммерческие настройки.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS category_settings (
                        category_id INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
                        bank_commission_type TEXT DEFAULT 'none',
                        bank_commission_value NUMERIC DEFAULT 0,
                        cancellation_policy TEXT DEFAULT 'no_refund',
                        premium_contact_enabled BOOLEAN DEFAULT FALSE,
                        premium_contact_fee NUMERIC DEFAULT 0,
                        premium_disclosure_scope TEXT DEFAULT 'none',
                        contact_reveal_after_booking BOOLEAN DEFAULT TRUE
                    )
                """)
                settings_fields = [
                    "bank_commission_type", "bank_commission_value",
                    "cancellation_policy", "premium_contact_enabled",
                    "premium_contact_fee", "premium_disclosure_scope",
                    "contact_reveal_after_booking",
                ]
                cur.execute("""
                    INSERT INTO category_settings (category_id) VALUES (%s)
                    ON CONFLICT (category_id) DO NOTHING
                """, (cat_id,))
                for field in settings_fields:
                    if field in data:
                        cur.execute(f"UPDATE category_settings SET {field} = %s WHERE category_id = %s", (data[field], cat_id))
                conn.commit()

    # ------------------------------------------------------------------
    # АДМИНИСТРАТИВНЫЙ CRUD УПРАВЛЕНИЯ КАТАЛОГОМ
    # ------------------------------------------------------------------
    def create_master_category(self, name_ru: str, name_am: str, slug: str) -> int:
        """Создать новую главную родительскую сферу."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO master_categories (name_ru, name_am, slug)
                    VALUES (%s, %s, %s) RETURNING id
                ''', (name_ru, name_am, slug))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def update_master_category(self, mcat_id: int, **kwargs):
        """Редактировать поля существующей главной сферы."""
        if not kwargs:
            return
        sets, vals = [], []
        for k, v in kwargs.items():
            sets.append(f"{k} = %s")
            vals.append(v)
        vals.append(mcat_id)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE master_categories SET {', '.join(sets)} WHERE id = %s", vals)
                conn.commit()

    def delete_master_category(self, mcat_id: int):
        """Удалить главную сферу. Связанные услуги удалятся автоматически благодаря ON DELETE CASCADE."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM master_categories WHERE id = %s", (mcat_id,))
                conn.commit()

    def create_subcategory(self, master_category_id: int, name_ru: str, name_am: str, slug: str, 
                           commission_type: str = 'on_top', commission_value: float = 10.0) -> int:
        """Создать новую дочернюю услугу внутри выбранной сферы."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO categories (master_category_id, name_ru, name_am, slug, commission_type, commission_value, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE) RETURNING id
                ''', (master_category_id, name_ru, name_am, slug, commission_type, commission_value))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def delete_subcategory(self, cat_id: int):
        """Полное удаление конкретной услуги из каталога."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM categories WHERE id = %s", (cat_id,))
                conn.commit()
    # ------------------------------------------------------------------
    # НАСТРОЙКИ ПРОФИЛЯ ПАРТНЕРА (СВЯЗЬ МАСТЕР ↔ ПОДКАТЕГОРИИ)
    # ------------------------------------------------------------------
    def set_master_categories(self, user_id: int, category_ids: list[int]):
        """Устанавливает специализации мастера в таблице связей master_skills."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM master_skills WHERE user_id = %s", (user_id,))
                for cat_id in category_ids:
                    cur.execute('''
                        INSERT INTO master_skills (user_id, category_id, is_active) 
                        VALUES (%s, %s, TRUE) 
                        ON CONFLICT DO NOTHING
                    ''', (user_id, cat_id))
                conn.commit()

    def get_master_categories(self, user_id: int) -> list[dict]:
        """Возвращает список всех подкатегорий, на которые подписан мастер."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT ms.*, c.name_am as name_hy, c.name_ru, c.slug, c.master_category_id
                    FROM master_skills ms
                    JOIN categories c ON ms.category_id = c.id
                    WHERE ms.user_id = %s AND ms.is_active = TRUE
                    ORDER BY c.id
                ''', (user_id,))
                return cur.fetchall()

    def toggle_master_category(self, user_id: int, category_id: int, is_active: bool = True):
        """Включает или выключает получение уведомлений по конкретному направлению."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    UPDATE master_skills 
                    SET is_active = %s 
                    WHERE user_id = %s AND category_id = %s
                ''', (is_active, user_id, category_id))
                conn.commit()

    # ------------------------------------------------------------------
    # ЗАКАЗЫ И ПОИСК ИСПОЛНИТЕЛЕЙ
    # ------------------------------------------------------------------
    def create_order(self, client_id: int, category_id: int, category_name_hy: str,
                     summary: str, checklist: list[str] = None,
                     city: str = "Ереван", lang: str = "hy") -> int:
        """Создает новый заказ в системе с привязкой к подкатегории."""
        cl_json = json.dumps(checklist or [], ensure_ascii=False)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO orders (client_id, category_id, category_name_hy, summary, checklist_json, city, lang, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'bidding')
                    RETURNING id
                ''', (client_id, category_id, category_name_hy, summary, cl_json, city, lang))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def get_order(self, order_id: int) -> dict | None:
        """Возвращает детальную информацию о заказе по его ID."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
                return cur.fetchone()

    def update_order_status(self, order_id: int, status: str):
        """Обновляет текущий статус заказа."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s",
                    (status, order_id),
                )
                conn.commit()

    def update_order_price(self, order_id: int, price: float):
        """Фиксирует финальную стоимость закрытия заказа."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET final_price = %s, updated_at = NOW() WHERE id = %s",
                    (price, order_id),
                )
                conn.commit()

    def find_matching_masters(self, category_name_or_slug: str, city: str) -> list[dict]:
        """Ищет верифицированных мастеров по категории и городу."""
        with _connect() as conn:
            with conn.cursor() as cur:
                city_lower = city.strip().lower()
                allowed = CITY_SYNONYMS.get(city_lower, [city_lower])
                cur.execute('''
                    SELECT u.telegram_id, u.username, u.city, u.rating_avg
                    FROM users u
                    JOIN master_skills ms ON u.telegram_id = ms.user_id
                    JOIN categories c ON ms.category_id = c.id
                    WHERE (c.name_am = %s OR c.name_ru = %s OR c.slug = %s)
                      AND u.role = 'master'
                      AND u.is_verified = TRUE
                      AND u.is_frozen = FALSE
                      AND ms.is_active = TRUE
                      AND TRIM(LOWER(u.city)) = ANY(%s)
                ''', (category_name_or_slug, category_name_or_slug, category_name_or_slug, allowed))
                return cur.fetchall()
    # ------------------------------------------------------------------
    # СТАВКИ / ТОРГИ (БИДЫ МАСТЕРОВ)
    # ------------------------------------------------------------------
    def create_bid(self, order_id: int, master_id: int, amount: float, message: str = "") -> int:
        """Создает ставку от мастера на заказ или обновляет её, если она уже существует."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO bids (order_id, master_id, amount, message)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (order_id, master_id) DO UPDATE SET amount = EXCLUDED.amount, message = EXCLUDED.message
                    RETURNING id
                ''', (order_id, master_id, amount, message))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def get_bids(self, order_id: int) -> list[dict]:
        """Возвращает все ставки (предложения) мастеров по конкретному заказу."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM bids WHERE order_id = %s ORDER BY created_at",
                    (order_id,),
                )
                return cur.fetchall()

    def accept_bid(self, order_id: int, master_id: int):
        """Принимает ставку выбранного мастера, отклоняя все остальные активные ставки."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bids SET status = 'accepted' WHERE order_id = %s AND master_id = %s",
                    (order_id, master_id),
                )
                cur.execute(
                    "UPDATE bids SET status = 'rejected' WHERE order_id = %s AND master_id != %s AND status = 'pending'",
                    (order_id, master_id),
                )
                conn.commit()

    def counter_bid(self, order_id: int, master_id: int, new_amount: float):
        """Отправляет встречное ценовое предложение (контр-оффер) по ставке."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bids SET status = 'counter', amount = %s WHERE order_id = %s AND master_id = %s",
                    (new_amount, order_id, master_id),
                )
                conn.commit()

    # ------------------------------------------------------------------
    # СДЕЛКИ И БЕЗОПАСНАЯ ОПЛАТА (ИНТЕГРАЦИЯ IDRAM)
    # ------------------------------------------------------------------
    def create_deal(self, deal_id: str, order_id: int, client_id: int, master_id: int,
                    secure_code: str, total_price: float, commission: float, master_payout: float):
        """Создает безопасную сделку после успешной оплаты клиентом заказа."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO deals (deal_id, order_id, client_id, master_id, secure_code,
                                       total_price, commission, master_payout)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (deal_id, order_id, client_id, master_id, secure_code,
                      total_price, commission, master_payout))
                conn.commit()

    def get_deal(self, deal_id: str) -> dict | None:
        """Возвращает данные о сделке по её уникальному текстовому ID."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM deals WHERE deal_id = %s", (deal_id,))
                return cur.fetchone()

    def update_deal_status(self, deal_id: str, status: str, idram_txn_id: str = None):
        """Обновляет статус сделки (например, при получении вебхука от Idram)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                if idram_txn_id:
                    cur.execute(
                        "UPDATE deals SET status = %s, idram_txn_id = %s WHERE deal_id = %s",
                        (status, idram_txn_id, deal_id),
                    )
                else:
                    cur.execute(
                        "UPDATE deals SET status = %s WHERE deal_id = %s",
                        (status, deal_id),
                    )
                conn.commit()

    def close_deal(self, deal_id: str):
        """Переводит сделку в финальный закрытый статус."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE deals SET status = 'closed', closed_at = NOW() WHERE deal_id = %s",
                    (deal_id,),
                )
                conn.commit()

    def verify_and_close_deal(self, deal_id: str, code: str, master_id: int) -> str:
        """Проверяет секретный код от клиента. Если код верный — закрывает сделку."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM deals WHERE deal_id = %s AND master_id = %s",
                    (deal_id, master_id),
                )
                deal = cur.fetchone()
        if not deal:
            return "not_found"
        if deal["status"] == "closed":
            return "already_closed"
        if deal["secure_code"] != code:
            return "bad_code"
        self.close_deal(deal_id)
        return "ok"
    # ------------------------------------------------------------------
    # АНОНИМНЫЙ ЧАТ И СИСТЕМА ОТЗЫВОВ
    # ------------------------------------------------------------------
    def save_chat_message(self, order_id: int, sender_id: int, receiver_id: int,
                          text: str, is_blocked: bool = False, block_reason: str = None):
        """Сохраняет сообщение анонимного чата по заказу в базу данных."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO chat_messages (order_id, sender_id, receiver_id, text, is_blocked, block_reason)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (order_id, sender_id, receiver_id, text, is_blocked, block_reason))
                conn.commit()

    def get_chat_history(self, order_id: int, limit: int = 50) -> list[dict]:
        """Возвращает историю переписки по заказу (скрывая заблокированные сообщения)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM chat_messages WHERE order_id = %s AND is_blocked = FALSE ORDER BY created_at LIMIT %s",
                    (order_id, limit),
                )
                return cur.fetchall()

    def create_review(self, deal_id: str, client_id: int, master_id: int, rating: int, comment: str = ""):
        """Создает отзыв о мастере и автоматически пересчитывает его средний рейтинг."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO reviews (deal_id, client_id, master_id, rating, comment)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (deal_id, client_id, master_id, rating, comment))
                # Формула динамического пересчета среднего рейтинга мастера
                cur.execute('''
                    UPDATE users SET
                        rating_count = rating_count + 1,
                        rating_avg = (rating_avg * rating_count + %s) / (rating_count + 1)
                    WHERE telegram_id = %s
                ''', (rating, master_id))
                conn.commit()

    # ------------------------------------------------------------------
    # АРБИТРАЖ (СПОРЫ) И СТАТИСТИКА ЛИЧНОГО КАБИНЕТА МАСТЕРА
    # ------------------------------------------------------------------
    def create_dispute(self, deal_id: str, initiated_by: int, reason: str) -> int:
        """Открывает спор по сделке в арбитраже маркетплейса."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO disputes (deal_id, initiated_by, reason)
                    VALUES (%s, %s, %s)
                    RETURNING id
                ''', (deal_id, initiated_by, reason))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def resolve_dispute(self, dispute_id: int, resolution: str, refund: bool = False):
        """Закрывает спор с вынесением решения и опциональным возвратом средств."""
        with _connect() as conn:
            with conn.cursor() as cur:
                status = 'refunded' if refund else 'resolved'
                cur.execute(
                    "UPDATE disputes SET status = %s, resolution = %s, resolved_at = NOW() WHERE id = %s",
                    (status, resolution, dispute_id),
                )
                conn.commit()

    def get_client_orders(self, client_id: int) -> list[dict]:
        """Возвращает историю всех заказов клиента для вывода в боте."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, category_name_hy, status, created_at
                    FROM orders WHERE client_id = %s
                    ORDER BY created_at DESC LIMIT 20
                ''', (client_id,))
                return cur.fetchall()

    def get_master_history(self, master_id: int) -> dict:
        """Собирает полную историю мастера: количество сделок, заработок, отзывы и историю для Web App."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT COUNT(*) as cnt,
                           COALESCE(SUM(master_payout),0) as earned,
                           COALESCE(SUM(commission),0) as comm
                    FROM deals WHERE master_id = %s AND status = 'closed'
                ''', (master_id,))
                stats = cur.fetchone()
                cur.execute('''
                    SELECT d.deal_id, d.master_payout as payout, d.closed_at as date,
                           o.category_name_hy as category
                    FROM deals d LEFT JOIN orders o ON d.order_id = o.id
                    WHERE d.master_id = %s AND d.status = 'closed'
                    ORDER BY d.closed_at DESC LIMIT 30
                ''', (master_id,))
                deals = cur.fetchall()
        user = self.get_user(master_id)
        return {
            "completed_count": stats["cnt"],
            "total_earned": float(stats["earned"]),
            "total_commission": float(stats["comm"]),
            "avg_rating": float(user["rating_avg"]) if user and user.get("rating_avg") else None,
            "deals": deals,
        }

    def get_active_chat_for_master(self, master_id: int) -> dict | None:
        """Ищет последний активный заказ мастера, по которому утвержден чат."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT b.order_id, o.client_id
                    FROM bids b JOIN orders o ON b.order_id = o.id
                    WHERE b.master_id = %s AND b.status = 'accepted'
                      AND o.status IN ('bidding', 'awaiting_payment', 'booked')
                    ORDER BY b.created_at DESC LIMIT 1
                ''', (master_id,))
                return cur.fetchone()
    # ------------------------------------------------------------------
    # ЛЕНТА ЗАЯВОК МАСТЕРА И ФУНКЦИОНАЛ АДМИН-ПАНЕЛИ (ADMIN.HTML)
    # ------------------------------------------------------------------
    def get_master_feed(self, master_id: int) -> list[dict]:
        """Возвращает новые заявки, подходящие мастеру по его подкатегориям из master_skills и городу."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT o.id, o.category_name_hy, o.summary, o.city, o.created_at,
                           c.name_ru as category_name, c.name_am as category_name_am
                    FROM orders o
                    LEFT JOIN categories c ON o.category_id = c.id
                    WHERE o.status = 'bidding'
                      AND o.category_id IN (
                          SELECT ms.category_id FROM master_skills ms
                          WHERE ms.user_id = %s AND ms.is_active = TRUE
                      )
                    ORDER BY o.created_at DESC LIMIT 20
                ''', (master_id,))
                rows = cur.fetchall()
                for r in rows:
                    r["time_ago"] = str(r.get("created_at", ""))[:16] if r.get("created_at") else ""
                return rows

    def get_all_users(self, limit: int = 100) -> list[dict]:
        """Возвращает список всех пользователей маркетплейса для админ-панели."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT %s", (limit,))
                return cur.fetchall()

    def get_all_orders(self, limit: int = 100) -> list[dict]:
        """Возвращает список всех заказов системы для админ-панели."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
                for r in rows:
                    r["category_name"] = r.get("category_name_hy", "")
                return rows

    def get_open_disputes(self) -> list[dict]:
        """Возвращает список всех открытых споров, требующих вмешательства модератора/админа."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM disputes WHERE status = 'open' ORDER BY created_at DESC")
                return cur.fetchall()

    def delete_user(self, telegram_id: int):
        """Полное удаление пользователя из системы администратором."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE telegram_id = %s", (telegram_id,))
                conn.commit()


    # ------------------------------------------------------------------
    # NEW AI-FIRST PARTNER CORE (kept here for legacy main.py compatibility)
    # ------------------------------------------------------------------
    def get_partner_by_user(self, user_id: int):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM partners WHERE user_id=%s LIMIT 1", (user_id,))
                return cur.fetchone()

    def create_partner(self, user_id: int) -> int:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO partners(user_id) VALUES(%s) ON CONFLICT(user_id) DO UPDATE SET updated_at=NOW() RETURNING id", (user_id,))
                row=cur.fetchone(); conn.commit(); return row["id"] if row else None

    def update_partner(self, partner_id: int, **kwargs):
        allowed={"business_name","business_description","status","verification_status","rejection_reason","contact_share_policy","profile_json"}
        data={k:v for k,v in kwargs.items() if k in allowed}
        if not data: return self.get_partner_by_id(partner_id)
        sets=[]; vals=[]
        for k,v in data.items():
            if k == "profile_json" and not isinstance(v,str):
                v=json.dumps(v,ensure_ascii=False)
                sets.append(f"{k}=%s::jsonb")
            else:
                sets.append(f"{k}=%s")
            vals.append(v)
        sets.append("updated_at=NOW()")
        vals.append(partner_id)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE partners SET {', '.join(sets)} WHERE id=%s RETURNING *", vals)
                row=cur.fetchone(); conn.commit(); return row

    def get_partner_by_id(self, partner_id: int):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM partners WHERE id=%s", (partner_id,))
                return cur.fetchone()

    def get_admin_stats(self) -> dict:
        """Собирает общую финансовую и операционную статистику маркетплейса для панели инструментов админа."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT
                        (SELECT COUNT(*) FROM users) as total_users,
                        (SELECT COUNT(*) FROM users WHERE role='master') as total_masters,
                        (SELECT COUNT(*) FROM users WHERE role='master' AND is_verified=TRUE) as verified_masters,
                        (SELECT COUNT(*) FROM orders) as total_orders,
                        (SELECT COUNT(*) FROM orders WHERE status='completed') as completed_orders,
                        (SELECT COALESCE(SUM(commission), 0) FROM deals WHERE status='closed') as total_commission,
                        (SELECT COUNT(*) FROM disputes WHERE status='open') as open_disputes
                ''')
                return cur.fetchone()
