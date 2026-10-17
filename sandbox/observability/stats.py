"""Лічильники в пам'яті процесу (W3+): обнуляються при рестарті сервісу — це очікувано."""

from dataclasses import dataclass


@dataclass
class Stats:
    hits: int = 0      # відповідь з кешу, модель не викликалась
    misses: int = 0    # кешу не було — пішли в модель


STATS = Stats()
