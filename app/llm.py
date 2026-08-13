"""Модель через OpenRouter.

Один вызов делает всю работу за раз: пишет ответ клиенту, вытаскивает поля
лида и решает, пора ли звать человека. Три отдельных запроса стоили бы втрое
дороже и втрое дольше, а решения всё равно принимаются по одному и тому же
куску переписки.

Без ключа модуль не падает — он возвращает пустой результат с флагом
handoff, и диалог уходит менеджеру. Это поведение из ТЗ: модель недоступна —
передать разговор человеку.
"""
from __future__ import annotations

import json
import logging

import httpx

from . import config, db, retrieval

log = logging.getLogger("llm")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"

# Ответ модели. Схему держим плоской — так модели ошибаются реже.
ANSWER_SCHEMA = """{
  "reply": "текст ответа клиенту",
  "handoff": true или false,
  "handoff_reason": "почему нужен человек, пусто если не нужен",
  "step_done": true или false,
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


async def _call(system: str, user: str, max_tokens: int = 900) -> str:
    key = api_key()
    if not key:
        return ""
    payload = {
        "model": current_model(),
        "max_tokens": max_tokens,
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
            resp.raise_for_status()
            return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("модель недоступна: %s", exc)
        return ""


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
    known = ", ".join(db.LEAD_STATUSES.values())

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
        "fields": {},
        "summary": "",
    }
    if not ai_ready():
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

    data = _parse(await _call(_system_prompt() + _script_block(contact_id), user))
    if not data:
        return fallback

    reply = str(data.get("reply") or "").strip()
    if not reply:
        # пустой ответ клиенту не отправляем никогда — лучше позвать человека
        return fallback

    fields = data.get("fields")
    return {
        "reply": reply,
        "handoff": bool(data.get("handoff")),
        "handoff_reason": str(data.get("handoff_reason") or "").strip(),
        "step_done": bool(data.get("step_done")),
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
    return await _call(system, _format_history(rows), max_tokens=200)
