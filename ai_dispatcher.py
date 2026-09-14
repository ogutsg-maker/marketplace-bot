"""
AIDispatcher — мозг платформы.
Groq (Llama-3) выполняет ВСЮ работу: и диспетчерскую, и модерацию.
Скорость Groq на процессорах — ультрабыстрая, идеально для чат-модерации.

OpenAI GPT-4o-mini зарегистрирован в requirements как резерв,
но основная нагрузка идёт через Groq для максимальной скорости.
"""
import os
import json
import logging
import asyncio
from groq import Groq
from pydantic import BaseModel, Field
from config import GROQ_API_KEY

logger = logging.getLogger(__name__)


# ─── Pydantic модели ─────────────────────────────────────────────

class ServiceRequestAnalysis(BaseModel):
    language: str = Field(description="Язык запроса: 'hy', 'ru' или 'en'")
    category_hy: str = Field(description="Категория СТРОГО на армянском (из базы categories.name_hy)")
    category_id: int | None = Field(default=None, description="ID категории из базы, если удалось определить")
    summary: str = Field(description="Краткое описание сути проблемы на русском")
    checklist: list[str] = Field(description="Чек-лист уточняющих вопросов (3-5) на ЯЗЫКЕ клиента")
    city: str = Field(description="Город на русском, если упомянут, иначе 'Ереван'")


class ModerationResult(BaseModel):
    is_safe: bool = Field(description="True если безопасно, False если есть контакты")
    reason: str = Field(description="Причина блокировки или 'Одобрено'")
    cleaned_text: str = Field(description="Текст без контактов (если is_safe=False, то цензурированный)")


class BidAnalysis(BaseModel):
    amount: float = Field(description="Извлечённая сумма в AMD")
    is_price_proposal: bool = Field(description="Является ли сообщение предложением цены")


# ─── Основной класс ──────────────────────────────────────────────

class AIDispatcher:
    def __init__(self):
        # Groq — основной, быстрый провайдер
        self.groq = Groq(api_key=GROQ_API_KEY)
        self.groq_model = "openai/gpt-oss-20b"

        # OpenAI отключён на текущем этапе. Основной AI работает через Groq.
        self.openai = None

    def _groq_chat(self, system: str, user: str, json_mode: bool = True) -> str:
        """Синхронный вызов Groq (быстрый, для запуска в executor)."""
        kwargs = dict(
            model=self.groq_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return self.groq.chat.completions.create(**kwargs).choices[0].message.content

    async def _call_groq(self, system: str, user: str, json_mode: bool = True) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._groq_chat(system, user, json_mode))

    # ─── 1. Анализ запроса клиента (Этап 2.1 ТЗ) ─────────────────

    async def analyze_request(self, user_text: str, categories_list: list[dict]) -> ServiceRequestAnalysis:
        """
        Определяет категорию, язык, город, чек-лист.
        categories_list — список из БД: [{id, name_hy, name_ru, name_en}, ...]
        """
        cats_desc = "\n".join(
            f"  id={c['id']}: {c['name_hy']} | {c['name_ru']} | {c['name_en']}"
            for c in categories_list
        )

        system = f"""Ты — ИИ-диспетчер маркетплейса локальных услуг в Армении.

Твоя задача:
1. Определи язык клиента: 'hy' (армянский/транслит), 'ru' (русский), 'en' (английский).
2. Определи категорию услуги СТРОГО из списка ниже (поле category_hy = name_hy, category_id = id).
3. Определи город клиента (если не указан — ставь 'Ереван').
4. Составь краткую выжимку сути на русском.
5. Составь чек-лист уточняющих вопросов (3-5) НА ЯЗЫКЕ клиента.

Доступные категории:
{cats_desc}

ОТВЕТЬ СТРОГО JSON:
{{
  "language": "hy|ru|en",
  "category_hy": "имя на армянском из списка",
  "category_id": число,
  "summary": "описание на русском",
  "checklist": ["вопрос1", "вопрос2"],
  "city": "Город на русском"
}}"""

        try:
            raw = await self._call_groq(system, user_text)
            parsed = json.loads(raw)
            return ServiceRequestAnalysis(**parsed)
        except Exception as e:
            logger.error(f"Ошибка анализа запроса: {e}")
            return ServiceRequestAnalysis(
                language="ru",
                category_hy="Ошибка API",
                category_id=None,
                summary="Не удалось разобрать ответ ИИ.",
                checklist=["Попробуйте отправить запрос ещё раз"],
                city="Ереван",
            )

    # ─── 2. Модерация сообщений (Этап 2.2 ТЗ) ────────────────────

    async def moderate_message(self, text: str) -> ModerationResult:
        """
        Groq-модератор: блокирует телефоны, ссылки, адреса.
        Исправляет текст, удаляя контакты, но сохраняя смысл.
        """
        system = """Ты — строгий ИИ-модератор маркетплейса услуг в Армении.

ПРАВИЛО: До оплаты комиссии любые контакты ЗАПРЕЩЕНЫ.
Блокируй если есть:
- Номера телефонов (093..., +374..., словами «ноль девяносто три» и т.д.)
- Ссылки (list.am, t.me, instagram.com, facebook.com, и т.п.)
- Telegram-ники (@username)
- Точные адреса (ул.某某, д.某某)

Если найдёшь контакты — цензурируй их, заменив на [УДАЛЕНО],
а is_safe поставь False.

ОТВЕТЬ СТРОГО JSON:
{
  "is_safe": true/false,
  "reason": "Причина блокировки на русском",
  "cleaned_text": "Текст с удалёнными контактами"
}"""

        try:
            raw = await self._call_groq(system, text)
            parsed = json.loads(raw)
            return ModerationResult(**parsed)
        except Exception as e:
            logger.error(f"Ошибка модерации: {e}")
            # В случае ошибки — пропускаем (fail open)
            return ModerationResult(is_safe=True, reason="Ошибка проверки", cleaned_text=text)

    # ─── 3. Извлечение цены из сообщения (Этап торга) ────────────

    async def extract_bid(self, text: str) -> BidAnalysis:
        """Извлекает предложенную сумму из сообщения мастера или клиента."""
        system = """Ты извлекаешь цену из сообщения в маркетплейсе услуг.
Если пользователь называет цену — верни её в AMD.
Если это не предложение цены — is_price_proposal = false.

ОТВЕТЬ СТРОГО JSON:
{
  "amount": число в AMD,
  "is_price_proposal": true/false
}"""

        try:
            raw = await self._call_groq(system, text)
            parsed = json.loads(raw)
            return BidAnalysis(**parsed)
        except Exception as e:
            logger.error(f"Ошибка извлечения ставки: {e}")
            return BidAnalysis(amount=0, is_price_proposal=False)

    # ─── 4. Whisper STT (через OpenAI) ────────────────────────────

    async def transcribe_voice(self, audio_path: str) -> str:
        """Транскрибация голосового сообщения через OpenAI Whisper."""
        if not self.openai:
            return "[Ошибка: OPENAI_API_KEY не задан]"
        loop = asyncio.get_event_loop()
        def _transcribe():
            with open(audio_path, "rb") as f:
                resp = self.openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                )
                return resp
        try:
            return await loop.run_in_executor(None, _transcribe)
        except Exception as e:
            logger.error(f"Ошибка Whisper STT: {e}")
            return "[Ошибка транскрибации]"

    # ─── 5. Встречное предложение (поторговаться) ────────────────

    async def generate_counter_offer(self, original_price: float, client_text: str, lang: str) -> str:
        """Генерирует встречное предложение на языке клиента."""
        lang_map = {"hy": "армянском", "ru": "русском", "en": "английском"}
        lang_name = lang_map.get(lang, "русском")
        system = f"""Ты — помощник клиента в маркетплейсе.
Клиент хочет поторговаться. Мастер предложил {original_price} AMD.
На основе сообщения клиента составь вежливое встречное предложение НА {lang_name} языке.
Ответь только текстом предложения, без JSON."""

        try:
            raw = await self._call_groq(system, client_text, json_mode=False)
            return raw.strip()
        except Exception as e:
            logger.error(f"Ошибка генерации контр-оффера: {e}")
            return f"Не могли бы вы сделать дешевле?"

    async def analyze_document_content(self, content: str, lang: str = "hy") -> str:
        """Extract useful business information from text extracted from a PDF/document."""
        system = f"""Դու Armenia AI Guide-ի փաստաթղթերի վերլուծիչն ես։ Լեզուն՝ {lang}.
Կարդա փաստաթղթի տեքստը և դուրս բեր միայն փաստաթղթում իրականում նշված տվյալները՝ բիզնեսի անուն, նկարագրություն, ուղղություն, ծառայություններ, գներ, հասցե/քաղաք, գրաֆիկ, փաթեթներ։ Ոչինչ մի հորինի։ Պատասխանիր նույն լեզվով պարզ կառուցվածքային տեքստով։"""
        try:
            return await self._call_groq(system, content[:20000], json_mode=False)
        except Exception as e:
            logger.error("Document analysis failed: %s", e)
            return content[:12000]


    async def analyze_image_content(self, image_bytes: bytes, mime_type: str = "image/jpeg", lang: str = "hy") -> str:
        """Optional vision analysis for business documents/photos. Uses OpenAI only when configured."""
        if not self.openai:
            return ""
        import base64
        payload=base64.b64encode(image_bytes).decode('ascii')
        loop=asyncio.get_event_loop()
        def _call():
            resp=self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role":"user",
                    "content":[
                        {"type":"text","text":f"Analyze this Armenian business document/photo. Extract only visible real information: business name, direction, services, prices, location, schedule and packages. Do not invent. Reply in language {lang}."},
                        {"type":"image_url","image_url":{"url":f"data:{mime_type};base64,{payload}"}}
                    ]
                }],
                max_tokens=1200
            )
            return resp.choices[0].message.content or ""
        try:
            return await loop.run_in_executor(None,_call)
        except Exception as e:
            logger.error("Image analysis failed: %s",e)
            return ""
