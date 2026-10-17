"""Стан агента в пам'яті. Окремий екземпляр від курсових лічильників: агентські збої
не повинні змінювати плитки консолі й /observability курсу."""

from dataclasses import asdict, dataclass


@dataclass
class AgentState:
    tasks: int = 0                 # скільки задач прогнано через конвеєр
    calls: int = 0                 # скільки викликів моделі зроблено
    call_errors: int = 0           # з них невдалих (503/429/0 ...)
    consecutive_failures: int = 0  # поспіль невдалих викликів; скидається першою відповіддю 200

    def record_call(self, ok: bool) -> None:
        # провайдери без стану: рахує той, хто викликає, — конвеєр агента
        self.calls += 1
        if ok:
            self.consecutive_failures = 0
        else:
            self.call_errors += 1
            self.consecutive_failures += 1

    def snapshot(self) -> dict:
        return asdict(self)


AGENT_STATE = AgentState()
