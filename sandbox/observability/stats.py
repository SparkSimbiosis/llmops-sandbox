"""Лічильники в пам'яті процесу (W3+): обнуляються при рестарті сервісу — це очікувано."""

from collections import Counter
from dataclasses import dataclass, field

ERROR_REASONS = ("provider_429", "provider_5xx", "network", "bad_json", "circuit_open",
                 "tool_failed", "guardrail_refusal", "garbage")


@dataclass
class Stats:
    hits: int = 0      # відповідь з кешу, модель не викликалась
    misses: int = 0    # кешу не було — пішли в модель
    semantic_hits: int = 0         # (опц.) з них — за схожістю, а не точним збігом
    fallback_events: int = 0       # перемикання на наступну модель ланцюга (W4)
    consecutive_failures: int = 0  # скільки разів поспіль упав увесь ланцюг (W4 -> /providers у W5)
    # (опц. W5) помилки за причинами — щоб при інциденті одразу бачити, що саме ламається
    errors: Counter = field(default_factory=lambda: Counter({r: 0 for r in ERROR_REASONS}))

    def count_failure(self, res) -> None:
        if res.error == "circuit_open":
            self.errors["circuit_open"] += 1
        elif res.error == "bad_json":
            self.errors["bad_json"] += 1
        elif res.status == 429:
            self.errors["provider_429"] += 1
        elif res.status >= 500:
            self.errors["provider_5xx"] += 1
        else:
            self.errors["network"] += 1

    def cache_hit_pct(self) -> float:
        total = self.hits + self.misses
        return round(100.0 * self.hits / total, 1) if total else 0.0


STATS = Stats()
