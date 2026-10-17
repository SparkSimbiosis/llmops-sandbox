"""Лічильники в пам'яті процесу (W3+): обнуляються при рестарті сервісу — це очікувано."""

from dataclasses import dataclass


@dataclass
class Stats:
    hits: int = 0      # відповідь з кешу, модель не викликалась
    misses: int = 0    # кешу не було — пішли в модель
    fallback_events: int = 0       # перемикання на наступну модель ланцюга (W4)
    consecutive_failures: int = 0  # скільки разів поспіль упав увесь ланцюг (W4 -> /providers у W5)


STATS = Stats()
