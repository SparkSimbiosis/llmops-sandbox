"""Інструменти, які модель може попросити (W3). Тут лише read-only; дії з наслідками (create_ticket) — hw4, через HITL."""

import logging
import secrets

log = logging.getLogger("sandbox.tools")


def lookup_order(arguments: str = "{}") -> str:
    # навчальний стенд: статус фіксований; mock шле порожні arguments, тож обов'язкових аргументів немає
    return "статус: оплачено, доставку призначено"


READ_ONLY_TOOLS = {"lookup_order": lookup_order}

# тікети, створені за ключем ідемпотентності: той самий ключ -> той самий тікет, другого не буде
TICKETS: dict[str, str] = {}


def create_ticket(idempotency_key: str) -> str:
    """Незворотна дія. Викликається ЛИШЕ з approve черги HITL, ніколи напряму з /chat."""
    if idempotency_key not in TICKETS:
        TICKETS[idempotency_key] = "тікет #T-" + secrets.token_hex(2)
        log.info("tool name=create_ticket created key=%s", idempotency_key)
    return TICKETS[idempotency_key]


def run_read_only(name: str, arguments: str = "{}") -> str | None:
    fn = READ_ONLY_TOOLS.get(name)
    if fn is None:
        return None
    result = fn(arguments)
    log.info("tool name=%s ok", name)
    return result
