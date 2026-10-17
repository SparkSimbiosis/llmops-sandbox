"""Черга підтверджень (W4, HITL): незворотна дія виконується лише після approve людиною.

Заявку «споживає» approve: її синхронно (без await до цього) прибирають із pending, тож два
одночасні approve не виконають дію двічі. Повторний або невідомий id -> 404: «вже виконано» і
«ніколи не існувало» навмисно не розрізняються, щоб відповідь не підказувала, які id існують.
"""

import secrets
from dataclasses import dataclass, field

from sandbox.tools.runtime import create_ticket


@dataclass
class Approval:
    id: str
    action: str
    request_id: str
    result: str | None = None


@dataclass
class ApprovalQueue:
    _pending: dict[str, Approval] = field(default_factory=dict)
    _completed: list[Approval] = field(default_factory=list)

    def add(self, action: str, request_id: str) -> Approval:
        item = Approval(id=secrets.token_hex(4), action=action, request_id=request_id)
        self._pending[item.id] = item
        return item

    def snapshot(self) -> dict:
        # форма для консолі: у pending немає result — дія ще не відбулась
        return {"pending": [{"id": a.id, "action": a.action} for a in self._pending.values()],
                "completed": [{"id": a.id, "action": a.action, "result": a.result} for a in self._completed]}

    def approve(self, approval_id: str) -> Approval | None:
        item = self._pending.pop(approval_id, None)   # атомарно: перевірка і видалення без await між ними
        if item is None:
            return None
        # ключ ідемпотентності — id заявки: та сама заявка ніколи не створить другий тікет
        item.result = create_ticket(idempotency_key=item.id)
        self._completed.append(item)
        return item


QUEUE = ApprovalQueue()
