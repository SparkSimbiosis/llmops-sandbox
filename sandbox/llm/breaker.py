"""Circuit breaker (W4, опційно): не смикати провайдера, який щойно впав кілька разів поспіль.

Стани для кожної моделі (аліасу):
- closed — звичайна робота;
- open — після FAIL_THRESHOLD збоїв поспіль модель «відкрита» на OPEN_S секунд: запит до неї не йде,
  сервіс одразу пробує наступну в ланцюгу або віддає заглушку;
- half-open — поки breaker відкритий, раз на PROBE_EVERY_S секунд пропускаємо один пробний запит.
  Успіх -> closed; збій -> знову open з новим вікном. Без half-open сервіс сам собі влаштував би
  outage на всі 30 с навіть після того, як провайдер ожив.
"""

import time
from dataclasses import dataclass, field

FAIL_THRESHOLD = 3
OPEN_S = 30.0
PROBE_EVERY_S = 5.0


@dataclass
class _State:
    failures: int = 0
    open_until: float = 0.0
    last_probe: float = 0.0


@dataclass
class CircuitBreaker:
    clock: callable = time.monotonic
    states: dict[str, _State] = field(default_factory=dict)

    def _st(self, alias: str) -> _State:
        return self.states.setdefault(alias, _State())

    def allow(self, alias: str) -> bool:
        """Чи можна зараз іти в модель: closed — так; open — лише пробний запит раз на PROBE_EVERY_S."""
        st = self._st(alias)
        now = self.clock()
        if st.failures < FAIL_THRESHOLD:
            return True
        if now >= st.open_until:
            return True                       # вікно минуло — пробуємо як half-open
        if now - st.last_probe >= PROBE_EVERY_S:
            st.last_probe = now               # половинчасте відкриття: один пробний запит
            return True
        return False

    def success(self, alias: str) -> None:
        self.states[alias] = _State()         # провайдер ожив — повністю закриваємо

    def failure(self, alias: str) -> None:
        st = self._st(alias)
        st.failures += 1
        if st.failures >= FAIL_THRESHOLD:
            now = self.clock()
            st.open_until = now + OPEN_S
            st.last_probe = now

    def is_open(self, alias: str) -> bool:
        st = self._st(alias)
        return st.failures >= FAIL_THRESHOLD and self.clock() < st.open_until


BREAKER = CircuitBreaker()
