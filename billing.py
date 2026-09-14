"""BillingManager — генерация QR-кодов, расчёт комиссий, интеграция Idram."""
import os
import random
import string
import qrcode
import io
import logging
from aiogram.types import BufferedInputFile
from database import DatabaseManager

logger = logging.getLogger(__name__)


class BillingManager:
    def __init__(self, db: DatabaseManager):
        self.db = db

    # ─── Генерация кодов ─────────────────────────────────────────

    @staticmethod
    def generate_deal_id() -> str:
        """Генерирует уникальный ID сделки (6 цифр)."""
        return "".join(random.choices(string.digits, k=6))

    @staticmethod
    def generate_secure_code() -> str:
        """Генерирует 6-значный резервный код."""
        return "".join(random.choices(string.digits, k=6))

    # ─── Расчёт комиссии ─────────────────────────────────────────

    def calculate_commission(self, category_id: int, price: float) -> dict:
        """
        Рассчитывает комиссию на основе настроек категории.
        Возвращает: {total, commission, master_payout, commission_type}
        """
        cats = self.db.get_all_categories()
        cat = next((c for c in cats if c["id"] == category_id), None)
        if not cat:
            return {"error": "Категория не найдена"}

        ct = cat["commission_type"]
        cv = float(cat["commission_value"])

        if ct == "inside":
            # Комиссия внутри цены (вычитается из тарифа мастера)
            commission = price * (cv / 100)
            master_payout = price - commission
            total = price
        elif ct == "on_top":
            # Комиссия сверх цены (добавляется к цене мастера)
            commission = price * (cv / 100)
            master_payout = price
            total = price + commission
        elif ct == "fixed":
            # Фиксированная сумма за бронирование
            commission = cv
            master_payout = price
            total = price + cv
        else:
            return {"error": "Неизвестный тип комиссии"}

        return {
            "total": round(total, 0),
            "commission": round(commission, 0),
            "master_payout": round(master_payout, 0),
            "commission_type": ct,
            "commission_value": cv,
            "category_name_hy": cat["name_hy"],
            "category_name_ru": cat["name_ru"],
        }

    # ─── QR-код ──────────────────────────────────────────────────

    @staticmethod
    def create_deal_qr(deal_id: str, secure_code: str, bot_username: str = None) -> BufferedInputFile:
        """
        Генерирует QR-код для закрытия сделки.
        Ссылка: https://t.me/<bot>?start=close_<deal_id>_<secure_code>
        """
        if bot_username:
            data = f"https://t.me/{bot_username}?start=close_{deal_id}_{secure_code}"
        else:
            data = f"DEAL:{deal_id}:CODE:{secure_code}"

        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        return BufferedInputFile(buf.getvalue(), filename=f"qr_{deal_id}.png")

    # ─── Чек бронирования ────────────────────────────────────────

    def format_booking_receipt(self, category_id: int, master_price: float, lang: str = "hy") -> str:
        """Формирует текст чека на языке клиента."""
        calc = self.calculate_commission(category_id, master_price)
        if "error" in calc:
            return "❌ Ошибка расчёта комиссии"

        if lang == "hy":
            return (
                f"🧾 **Ամրագրման Չակերգ / Чек бронирования**\n\n"
                f"📋 Ծառայություն: {calc['category_name_hy']}\n"
                f"💰 Մասնագիտական գին / Цена мастера: **{master_price:,.0f} ֏**\n"
                f"🏦 Հարթակի միջնորդավճար / Комиссия: **{calc['commission']:,.0f} ֏**\n"
                f"    ({calc['commission_type']} — {calc['commission_value']}{'%' if calc['commission_type'] != 'fixed' else ' ֏'})\n\n"
                f"💳 Վճարել հիմա / Оплатить сейчас: **{calc['commission']:,.0f} ֏**\n"
                f"🤝 Վարպետին տեղում / Мастеру на месте: **{calc['master_payout']:,.0f} ֏**\n"
                f"📦 Ընդհանուր / Итого: **{calc['total']:,.0f} ֏**"
            )
        elif lang == "en":
            return (
                f"🧾 **Booking Receipt**\n\n"
                f"📋 Service: {calc['category_name_ru']}\n"
                f"💰 Master price: **{master_price:,.0f} AMD**\n"
                f"🏦 Platform commission: **{calc['commission']:,.0f} AMD**\n"
                f"    ({calc['commission_type']} — {calc['commission_value']}{'%' if calc['commission_type'] != 'fixed' else ' AMD'})\n\n"
                f"💳 Pay now: **{calc['commission']:,.0f} AMD**\n"
                f"🤝 Pay master on-site: **{calc['master_payout']:,.0f} AMD**\n"
                f"📦 Total: **{calc['total']:,.0f} AMD**"
            )
        else:  # ru
            return (
                f"🧾 **Чек бронирования**\n\n"
                f"📋 Услуга: {calc['category_name_ru']}\n"
                f"💰 Цена мастера: **{master_price:,.0f} ֏**\n"
                f"🏦 Комиссия платформы: **{calc['commission']:,.0f} ֏**\n"
                f"    ({calc['commission_type']} — {calc['commission_value']}{'%' if calc['commission_type'] != 'fixed' else ' ֏'})\n\n"
                f"💳 Оплатить сейчас: **{calc['commission']:,.0f} ֏**\n"
                f"🤝 Мастеру на месте: **{calc['master_payout']:,.0f} ֏**\n"
                f"📦 Итого: **{calc['total']:,.0f} ֏**"
            )