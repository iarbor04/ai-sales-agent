"""Разговор с моделью: промпт, разбор ответа, решение о передаче человеку.

Провайдер выбирается в настройках — OpenRouter или YandexGPT — и
живёт в app/providers/. Этому модулю всё равно, чей API за ним.

Один вызов делает всю работу за раз: пишет ответ клиенту, вытаскивает поля
лида и решает, пора ли звать человека. Три отдельных запроса стоили бы втрое
дороже и втрое дольше, а решения всё равно принимаются по одному и тому же
куску переписки.

Без ключа модуль не падает — он возвращает пустой результат с флагом
handoff, и диалог уходит менеджеру. Это поведение из ТЗ: модель недоступна —
передать разговор человеку. Причина отказа при этом сохраняется дословно:
менеджер должен видеть «OpenRouter отклонил доступ (401)», а не догадываться.
"""
from __future__ import annotations

import json
import logging
import time

from . import db, providers, retrieval
from .providers import base

log = logging.getLogger("llm")

# Провайдер выбирается в настройках: OpenRouter или YandexGPT.
# Здесь остаются промпт, разбор ответа и повторные попытки — они одинаковы для
# всех, а транспорт живёт в app/providers/.
LLMError = base.LLMError
LLMTruncated = base.LLMTruncated


# Рассуждающие модели тратят на размышления тот же бюджет, что и на ответ:
# gpt-5 уходило 768 токенов на рассуждения из 900, и JSON обрывался на
# середине. Лимит — это потолок, а не предоплата: платится только за
# использованное, поэтому запас ничего не стоит.
ANSWER_TOKENS = 2500
SUMMARY_TOKENS = 800

# Напоминание для второй попытки: слабые модели забывают про формат.
JSON_REMINDER = (
    "\n\nВАЖНО: предыдущий ответ был отклонён, потому что это не JSON. "
    "Верни ровно один JSON-объект по описанной схеме, без markdown, "
    "без пояснений до и после."
)

# Ответ модели. Схему держим плоской — так модели ошибаются реже.
ANSWER_SCHEMA = """{
  "reply": "текст ответа клиенту",
  "handoff": true или false,
  "handoff_reason": "почему нужен человек, пусто если не нужен",
  "step_done": true или false,
  "booking": null или {"at": "ДД.ММ ЧЧ:ММ", "service": "название услуги"},
  "fields": {
    "name": "", "contact": "", "product": "", "need": "", "deadline": "", "comment": ""
  },
  "summary": "1-2 предложения: что нужно клиенту и о чём договорились"
}"""


def provider():
    return providers.current()


def ai_ready() -> bool:
    """Доступ к модели настроен. У каждого провайдера свой набор полей."""
    return provider().configured()


def current_model() -> str:
    active = provider()
    return db.setting("model", active.DEFAULT_MODEL) or active.DEFAULT_MODEL


async def available_models() -> list[dict]:
    """Список моделей выбранного провайдера — для выпадающего списка в панели."""
    try:
        return await provider().models()
    except Exception as exc:  # noqa: BLE001 — список моделей не критичен
        log.warning("список моделей недоступен: %s", exc)
        return []


async def _call(system: str, user: str, max_tokens: int = ANSWER_TOKENS,
                strict: bool = True) -> str:
    """Один запрос к модели. Ошибку не проглатывает, а объясняет.

    Раньше любая неудача возвращала пустую строку, и владелец видел только
    «модель недоступна» — по этой фразе нельзя отличить отозванный ключ от
    пустого счёта или недоступной модели.
    """
    active = provider()
    model = current_model()
    text, finish = await active.complete(system, user, model, max_tokens)

    # Обрезанный ответ выглядел как «модель ответила не по формату», и владелец
    # искал причину в модели, а не в лимите.
    if strict and finish == "length":
        raise LLMTruncated(
            f"ответ модели «{model}» не поместился в лимит {max_tokens} токенов")
    if not text:
        raise LLMError(f"модель «{model}» вернула пустой ответ")
    return text


_key_cache: tuple[str, float, dict] | None = None
KEY_CACHE_SECONDS = 120


async def check_key(force: bool = False) -> dict:
    """Проверка ключа там, где он действительно нужен.

    Список моделей OpenRouter отдаёт публично, поэтому «модели загрузились»
    не доказывает, что ключ рабочий. Этот эндпоинт без ключа не отвечает.

    Результат держим пару минут: страница настроек открывается часто, а ключ
    меняется редко. Кнопка проверки просит свежий ответ через force.
    """
    global _key_cache
    active = provider()
    if not active.configured():
        return {"ok": False, "detail": f"доступ к {active.TITLE} не настроен"}
    # ключом кеша служит имя провайдера вместе с его полями: сменили — проверим заново
    key = active.NAME + "|" + "|".join(db.setting(field[0], "") for field in active.FIELDS)
    now = time.monotonic()
    if not force and _key_cache and _key_cache[0] == key and now - _key_cache[1] < KEY_CACHE_SECONDS:
        return _key_cache[2]
    result = await provider().check_credentials()
    _key_cache = (key, now, result)
    return result


async def check_model() -> dict:
    """Живой пробный запрос выбранной моделью — то же, что делает агент."""
    model = current_model()
    try:
        # Запас на рассуждения: с коротким лимитом рассуждающая модель не
        # успевает даже начать ответ, и проверка врала бы про поломку.
        reply = await _call("Ответь одним словом: ок.", "Проверка связи.", max_tokens=600)
    except LLMError as exc:
        return {"ok": False, "model": model, "detail": str(exc)}
    if not reply:
        return {"ok": False, "model": model, "detail": f"модель «{model}» вернула пустой ответ"}
    return {"ok": True, "model": model, "detail": f"модель «{model}» ответила: {reply[:80]}"}


def _parse(raw: str) -> dict | None:
    """Достать JSON из ответа, даже если модель обернула его в markdown."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        log.warning("модель вернула не-JSON: %s", text[:200])
        return None


def _script_block(contact_id: int) -> str:
    """Текущий шаг сценария — цель, к которой агент ведёт разговор.

    Сценарий не превращает бота в анкету: модель получает цель шага и сама
    решает, как её достичь и достигнута ли она. Порядок шагов задаёт владелец
    в панели, раздел «Сценарий».
    """
    contact = db.contact_by_id(contact_id)
    if contact is None:
        return ""
    row = db.bot(contact["bot_id"]) if contact["bot_id"] else None
    if row is not None and not row["script_enabled"]:
        return ""

    steps = db.script(contact["bot_id"])
    if not steps:
        return ""

    index = min(contact["step"], len(steps) - 1)
    step = steps[index]
    plan = " → ".join(s["title"] for s in steps)

    return (
        f"\n\nСЦЕНАРИЙ РАЗГОВОРА: {plan}\n"
        f"Сейчас шаг {index + 1} из {len(steps)} — «{step['title']}».\n"
        f"Цель шага: {step['goal'] or step['title']}\n"
        "Веди разговор к цели этого шага. Как только она достигнута, поставь "
        "step_done = true — тогда следующим сообщением пойдёт следующий шаг. "
        "Не перескакивай вперёд и не возвращайся назад без нужды."
    )


def _system_prompt() -> str:
    business = db.setting("business_name", "").strip()
    tone = db.setting("tone", "").strip()
    known = ", ".join(db.stage_titles().values())

    # Свои правила владельца встают между поведением и схемой ответа: так он
    # может добавить что угодно про свой бизнес, но не сломает формат JSON и не
    # отменит передачу менеджеру — без них агент перестаёт отвечать вовсе.
    extra = db.setting("prompt_extra", "").strip()
    own = f"\n\nПРАВИЛА КОМПАНИИ (важнее общих советов выше):\n{extra}" if extra else ""

    return f"""Ты — продавец-консультант компании {business or "клиента"}.
Общаешься с клиентом в мессенджере от лица компании.

ГЛАВНОЕ ПРАВИЛО: отвечай ТОЛЬКО по информации из блока «БАЗА ЗНАНИЙ».
Никогда не выдумывай цены, наличие, сроки, условия, гарантии и скидки.
Если точного ответа в базе знаний нет — не угадывай и не отвечай общими
словами, а ставь handoff = true.

Как вести разговор:
- Пиши коротко, {tone or "по-человечески и по делу"}. Два-три предложения.
- За одно сообщение задавай НЕ БОЛЬШЕ ОДНОГО вопроса.
- Отвечай на языке клиента.
- Постепенно собери: имя, контакт, что нужно, к какому сроку, комментарий.
- Не дави и не повторяй вопрос, на который уже получил ответ.
- Это разговор, а не анкета. Сначала дай пользу — ответь на вопрос клиента,
  и только потом спрашивай сам.
- ВСЕГО за разговор задай не больше пяти уточняющих вопросов. Каждый лишний
  вопрос увеличивает шанс, что клиент просто уйдёт. Собрал главное —
  ставь handoff = true и передавай менеджеру.
- Не спрашивай про бюджет первым: этот вопрос в начале разговора отпугивает.

Когда ставить handoff = true:
- клиент просит человека, менеджера, живого сотрудника;
- клиент готов покупать, обсуждает оплату или договор;
- клиент жалуется или недоволен;
- просит индивидуальные условия, скидку, особые сроки;
- задал вопрос, ответа на который нет в базе знаний.

В поля fields клади только то, что клиент сказал явно. Ничего не додумывай.
Пустое поле — пустая строка. Статусы лида в системе: {known}.{own}

Ответ верни СТРОГО одним JSON-объектом такой формы, без markdown и пояснений:
{ANSWER_SCHEMA}"""


def prompt_preview(contact_id: int | None = None) -> str:
    """Ровно то, что уходит в модель системным сообщением.

    Промпт собирается из настроек, этапов воронки, шага сценария и правил
    компании — увидеть его целиком иначе было негде, приходилось читать код.
    """
    from . import booking as booking_mod

    prompt = _system_prompt()
    if contact_id:
        prompt += _script_block(contact_id)
    elif db.script():
        steps = db.script()
        plan = " → ".join(step["title"] for step in steps)
        prompt += (f"\n\nСЦЕНАРИЙ РАЗГОВОРА: {plan}\n"
                   f"Сейчас шаг 1 из {len(steps)} — «{steps[0]['title']}».\n"
                   f"Цель шага: {steps[0]['goal'] or steps[0]['title']}\n"
                   "Веди разговор к цели этого шага…")
    return prompt + booking_mod.slots_for_prompt()


def _format_history(rows: list) -> str:
    lines = []
    for row in rows:
        if row["author"] == "system":
            continue
        who = {"client": "Клиент", "ai": "Ты", "manager": "Менеджер"}.get(row["author"], "Мы")
        body = row["text"] or f"[{row['media_type'] or 'вложение'}]"
        lines.append(f"{who}: {body}")
    return "\n".join(lines)


async def answer(contact_id: int, question: str) -> dict:
    """Ответ клиенту + извлечённые поля + решение о передаче человеку.

    Всегда возвращает словарь. Если модель недоступна или ответила мусором —
    handoff = true, чтобы клиент не остался без ответа.
    """
    fallback = {
        "reply": "",
        "handoff": True,
        "handoff_reason": "модель недоступна",
        "step_done": False,
        "booking": None,
        "fields": {},
        "summary": "",
    }
    if not ai_ready():
        fallback["handoff_reason"] = "не задан ключ OpenRouter"
        return fallback

    context = retrieval.context_for(question)
    lead = db.get_lead(contact_id)
    already = ""
    if lead:
        collected = {
            key: lead[key] for key in db.LEAD_FIELDS
            if key != "summary" and lead[key]
        }
        if collected:
            already = "\nУЖЕ ИЗВЕСТНО О КЛИЕНТЕ (не спрашивай это повторно):\n" + json.dumps(
                collected, ensure_ascii=False
            )

    user = (
        "БАЗА ЗНАНИЙ:\n"
        + (context or "(пусто — база знаний не заполнена)")
        + already
        + "\n\nПЕРЕПИСКА:\n"
        + _format_history(db.history(contact_id, limit=24))
        + f"\n\nПоследнее сообщение клиента: {question}\n\nОтветь JSON-объектом."
    )

    from . import booking as booking_mod
    system = _system_prompt() + _script_block(contact_id) + booking_mod.slots_for_prompt()
    try:
        raw = await _call(system, user)
    except LLMTruncated as exc:
        # Рассуждения оказались длиннее ожидаемого — даём вдвое больше запаса.
        log.info("%s, пробуем с большим запасом", exc)
        try:
            raw = await _call(system, user, max_tokens=ANSWER_TOKENS * 2)
        except LLMError as inner:
            fallback["handoff_reason"] = str(inner)
            return fallback
    except LLMError as exc:
        fallback["handoff_reason"] = str(exc)
        return fallback

    data = _parse(raw)
    if not data:
        # Дешёвые модели часто отвечают обычным текстом вместо JSON. Одна
        # повторная попытка с прямым напоминанием спасает такой диалог и стоит
        # дешевле, чем потерянный клиент.
        log.info("модель ответила не по формату, пробуем ещё раз")
        try:
            raw = await _call(system + JSON_REMINDER, user)
        except LLMError as exc:
            fallback["handoff_reason"] = str(exc)
            return fallback
        data = _parse(raw)
    if not data:
        fallback["handoff_reason"] = f"модель «{current_model()}» ответила не по формату"
        return fallback

    reply = str(data.get("reply") or "").strip()
    if not reply:
        # пустой ответ клиенту не отправляем никогда — лучше позвать человека
        fallback["handoff_reason"] = f"модель «{current_model()}» вернула пустой ответ"
        return fallback

    fields = data.get("fields")
    return {
        "reply": reply,
        "handoff": bool(data.get("handoff")),
        "handoff_reason": str(data.get("handoff_reason") or "").strip(),
        "step_done": bool(data.get("step_done")),
        "booking": data.get("booking") if isinstance(data.get("booking"), dict) else None,
        "fields": fields if isinstance(fields, dict) else {},
        "summary": str(data.get("summary") or "").strip(),
    }


async def summarize(contact_id: int) -> str:
    """Короткое резюме диалога для карточки лида при передаче менеджеру."""
    rows = db.history(contact_id, limit=40)
    if not rows:
        return ""
    system = (
        "Сожми переписку в 1-2 предложения: что нужно клиенту и о чём договорились. "
        "Только факты из переписки, без выдумок. Верни голый текст без markdown."
    )
    try:
        # Резюме — обычный текст: обрезанное всё равно полезнее пустого.
        return await _call(system, _format_history(rows),
                           max_tokens=SUMMARY_TOKENS, strict=False)
    except LLMError as exc:
        # резюме — не главное: без него карточка лида просто уйдёт короче
        log.warning("резюме не составлено: %s", exc)
        return ""
