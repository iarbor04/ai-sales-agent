"""Модель через OpenRouter.

Один вызов делает всю работу за раз: пишет ответ клиенту, вытаскивает поля
лида и решает, пора ли звать человека. Три отдельных запроса стоили бы втрое
дороже и втрое дольше, а решения всё равно принимаются по одному и тому же
куску переписки.

Без ключа модуль не падает — он возвращает пустой результат с флагом
handoff, и диалог уходит менеджеру. Это поведение из ТЗ: модель недоступна —
передать разговор человеку. Причина отказа при этом сохраняется дословно:
менеджер должен видеть «OpenRouter отклонил ключ (401)», а не догадываться.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from . import config, db, retrieval

log = logging.getLogger("llm")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"


class LLMError(RuntimeError):
    """Понятная причина, почему модель не ответила.

    Текст этой ошибки уходит менеджеру в уведомление и в журнал обращений,
    поэтому он написан для человека, а не для разработчика.
    """


class LLMTruncated(LLMError):
    """Ответ обрезан лимитом токенов — значит, JSON пришёл неполным."""


# Рассуждающие модели тратят на размышления тот же бюджет, что и на ответ:
# gpt-5 уходило 768 токенов на рассуждения из 900, и JSON обрывался на
# середине. Лимит — это потолок, а не предоплата: платится только за
# использованное, поэтому запас ничего не стоит.
ANSWER_TOKENS = 2500
SUMMARY_TOKENS = 800

# Глубокие размышления в заполнении шаблона ответа не нужны и только съедают
# бюджет. Параметр понимают и OpenAI, и Anthropic, и Google, и DeepSeek;
# модели без рассуждений его просто игнорируют.
REASONING = {"effort": "low"}

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


def api_key() -> str:
    """Ключ из панели, а если там пусто — из .env.

    Владельцу удобнее вставить ключ в Настройках, чем лезть в файл на сервере,
    поэтому панель главнее. Ключ используется в каждом запросе заново, так что
    его смена работает сразу, без перезапуска службы.
    """
    return (db.setting("openrouter_key", "").strip() or config.OPENROUTER_API_KEY).strip()


def ai_ready() -> bool:
    return bool(api_key())


def current_model() -> str:
    return db.setting("model", config.OPENROUTER_MODEL) or config.OPENROUTER_MODEL


async def available_models() -> list[dict]:
    """Список моделей OpenRouter для выпадающего списка в Настройках."""
    if not ai_ready():
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                MODELS_URL,
                headers={"Authorization": f"Bearer {api_key()}"},
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("список моделей недоступен: %s", exc)
        return []

    models = [
        {"id": item.get("id", ""), "name": item.get("name", item.get("id", ""))}
        for item in data
        if item.get("id")
    ]
    models.sort(key=lambda m: m["id"])
    return models


def _error_text(resp: httpx.Response) -> str:
    """Что именно ответил OpenRouter.

    Одного кода состояния мало: 401 при отозванном ключе и 401 при ключе от
    другого аккаунта выглядят одинаково, а сообщение провайдера их различает.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return _tidy(str(error["message"]))
        if isinstance(error, str):
            return _tidy(error)
    return _tidy(resp.text)


def _tidy(detail: str) -> str:
    """Без хвостовой точки: текст подставляется в середину предложения."""
    return detail.strip().rstrip(".").strip()[:200]


def _failure(status: int, detail: str, model: str) -> str:
    detail = (detail or "").strip()
    tail = f": {detail}" if detail else ""
    if status == 401:
        return f"OpenRouter отклонил ключ (401){tail or ': ключ недействителен или отозван'}"
    if status == 402:
        return f"на счёте OpenRouter нет средств (402){tail}"
    if status == 403:
        return f"OpenRouter запретил запрос (403){tail}"
    if status == 404:
        return f"модель «{model}» недоступна по этому ключу (404){tail}"
    if status == 429:
        return f"OpenRouter ограничил частоту запросов (429){tail}"
    if status >= 500:
        return f"OpenRouter временно недоступен ({status}){tail}"
    return f"OpenRouter вернул ошибку {status}{tail}"


async def _call(system: str, user: str, max_tokens: int = ANSWER_TOKENS,
                strict: bool = True) -> str:
    """Один запрос к модели. Ошибку не проглатывает, а объясняет.

    Раньше любая неудача возвращала пустую строку, и владелец видел только
    «модель недоступна» — по этой фразе нельзя отличить отозванный ключ от
    пустого счёта или недоступной модели.
    """
    key = api_key()
    model = current_model()
    if not key:
        raise LLMError("не задан ключ OpenRouter")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "reasoning": REASONING,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise LLMError("OpenRouter не ответил вовремя") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"не удалось связаться с OpenRouter: {exc}") from exc

    if resp.status_code >= 400:
        reason = _failure(resp.status_code, _error_text(resp), model)
        log.error("запрос к модели не прошёл — %s", reason)
        raise LLMError(reason)

    try:
        body = resp.json()
    except ValueError as exc:
        raise LLMError("OpenRouter вернул не-JSON") from exc

    # OpenRouter умеет отвечать 200 с телом-ошибкой — это тоже отказ.
    error = body.get("error") if isinstance(body, dict) else None
    if error:
        detail = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        reason = _failure(int(code) if isinstance(code, int) else 400, str(detail or ""), model)
        log.error("запрос к модели не прошёл — %s", reason)
        raise LLMError(reason)

    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"модель «{model}» вернула пустой ответ")
    text = (choices[0].get("message", {}).get("content") or "").strip()

    # Обрезанный ответ выглядел как «модель ответила не по формату», и владелец
    # искал причину в модели, а не в лимите.
    if strict and choices[0].get("finish_reason") == "length":
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
    key = api_key()
    if not key:
        return {"ok": False, "detail": "ключ не задан"}
    now = time.monotonic()
    if not force and _key_cache and _key_cache[0] == key and now - _key_cache[1] < KEY_CACHE_SECONDS:
        return _key_cache[2]
    result = await _probe_key(key)
    _key_cache = (key, now, result)
    return result


async def _probe_key(key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(KEY_URL, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"OpenRouter недоступен: {exc}"}

    if resp.status_code >= 400:
        return {"ok": False, "detail": _failure(resp.status_code, _error_text(resp), current_model())}

    data = {}
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        pass
    detail = "ключ принят OpenRouter"
    limit, usage = data.get("limit"), data.get("usage")
    if limit is not None:
        detail += f", лимит {limit}, потрачено {usage or 0}"
    elif usage is not None:
        detail += f", потрачено {usage}"
    return {"ok": True, "detail": detail}


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
Пустое поле — пустая строка. Статусы лида в системе: {known}.

Ответ верни СТРОГО одним JSON-объектом такой формы, без markdown и пояснений:
{ANSWER_SCHEMA}"""


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
