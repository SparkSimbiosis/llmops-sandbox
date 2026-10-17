"""Черга підтверджень (W4, HITL): незворотна дія виконується лише після approve людиною.

Заявку «споживає» approve: її синхронно (без await до цього) прибирають із pending, тож два
одночасні approve не виконають дію двічі. Повторний або невідомий id -> 404: «вже виконано» і
«ніколи не існувало» навмисно не розрізняються, щоб відповідь не підказувала, які id існують.
"""

import os
import secrets
import time
from dataclasses import dataclass, field

from sandbox.tools.runtime import create_ticket


@dataclass
class Approval:
    id: str
    action: str
    request_id: str
    result: str | None = None
    created_at: float = 0.0


# (опц.) TTL: заявка, яку ніхто не підтвердив за APPROVAL_TTL_S секунд, переходить у expired;
# approve на неї відповідає як на виконану (404). Реальний HITL не має тримати заявки вічно.
APPROVAL_TTL_S = float(os.getenv("APPROVAL_TTL_S", "900"))


@dataclass
class ApprovalQueue:
    _pending: dict[str, Approval] = field(default_factory=dict)
    _completed: list[Approval] = field(default_factory=list)
    _expired: list[Approval] = field(default_factory=list)
    ttl_s: float = APPROVAL_TTL_S
    clock: callable = time.monotonic

    def _expire(self) -> None:
        now = self.clock()
        for aid in [a.id for a in self._pending.values() if now - a.created_at > self.ttl_s]:
            self._expired.append(self._pending.pop(aid))

    def add(self, action: str, request_id: str) -> Approval:
        item = Approval(id=secrets.token_hex(4), action=action, request_id=request_id, created_at=self.clock())
        self._pending[item.id] = item
        return item

    def snapshot(self) -> dict:
        # форма для консолі: у pending немає result — дія ще не відбулась
        self._expire()
        return {"expired": [{"id": a.id, "action": a.action} for a in self._expired],
                "pending": [{"id": a.id, "action": a.action} for a in self._pending.values()],
                "completed": [{"id": a.id, "action": a.action, "result": a.result} for a in self._completed]}

    def approve(self, approval_id: str) -> Approval | None:
        self._expire()                                  # протухла заявка вже не в pending -> 404
        item = self._pending.pop(approval_id, None)   # атомарно: перевірка і видалення без await між ними
        if item is None:
            return None
        # ключ ідемпотентності — id заявки: та сама заявка ніколи не створить другий тікет
        item.result = create_ticket(idempotency_key=item.id)
        self._completed.append(item)
        return item


QUEUE = ApprovalQueue()
