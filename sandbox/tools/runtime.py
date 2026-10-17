"""Інструменти, які модель може попросити (W3). Тут лише read-only; дії з наслідками (create_ticket) — hw4, через HITL."""

import logging

log = logging.getLogger("sandbox.tools")


def lookup_order(arguments: str = "{}") -> str:
    # навчальний стенд: статус фіксований; mock шле порожні arguments, тож обов'язкових аргументів немає
    return "статус: оплачено, доставку призначено"


READ_ONLY_TOOLS = {"lookup_order": lookup_order}


def run_read_only(name: str, arguments: str = "{}") -> str | None:
    fn = READ_ONLY_TOOLS.get(name)
    if fn is None:
        return None
    result = fn(arguments)
    log.info("tool name=%s ok", name)
    return result
