"""Інструменти, які модель може попросити (W3). Тут лише read-only; дії з наслідками (create_ticket) — hw4, через HITL."""

import asyncio
import json
import logging
import secrets

from sandbox.core.config import TOOL_TIMEOUT_S

log = logging.getLogger("sandbox.tools")


def lookup_order(arguments: dict) -> str:
    # навчальний стенд: статус фіксований; mock шле порожні arguments, тож обов'язкових аргументів немає
    return "статус: оплачено, доставку призначено"


READ_ONLY_TOOLS = {"lookup_order": lookup_order}
# (опц. hw3) схема аргументів: дозволені ключі й тип; unknown key / не той тип / битий JSON -> відмова, не виконання
TOOL_ARGS = {"lookup_order": {"order_id": str}}
TOOL_FAILED_TEXT = "не вдалося перевірити статус, спробуйте пізніше"


def validate_args(name: str, raw: str) -> dict | None:
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return None
    schema = TOOL_ARGS.get(name, {})
    if not isinstance(args, dict) or any(k not in schema or not isinstance(v, schema[k]) for k, v in args.items()):
        return None
    return args


async def run_read_only(name: str, arguments: str = "{}") -> str | None:
    fn = READ_ONLY_TOOLS.get(name)
    if fn is None:
        return None
    args = validate_args(name, arguments)
    if args is None:
        log.warning("tool name=%s refused: invalid arguments", name)   # запис про відмову, а не виконання
        return TOOL_FAILED_TEXT
    try:
        # (опц. hw3) timeout: інструмент — частина відповіді, краще чесно «не вдалося», ніж тримати чат
        result = await asyncio.wait_for(asyncio.to_thread(fn, args), timeout=TOOL_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("tool name=%s timeout after %.1fs", name, TOOL_TIMEOUT_S)
        return TOOL_FAILED_TEXT
    log.info("tool name=%s ok", name)
    return result


# тікети, створені за ключем ідемпотентності: той самий ключ -> той самий тікет, другого не буде
TICKETS: dict[str, str] = {}


def create_ticket(idempotency_key: str) -> str:
    """Незворотна дія. Викликається ЛИШЕ з approve черги HITL, ніколи напряму з /chat."""
    if idempotency_key not in TICKETS:
        TICKETS[idempotency_key] = "тікет #T-" + secrets.token_hex(2)
        log.info("tool name=create_ticket created key=%s", idempotency_key)
    return TICKETS[idempotency_key]

