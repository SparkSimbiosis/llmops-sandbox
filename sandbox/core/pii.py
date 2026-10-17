"""Guardrail PII (W4): email у вхідному повідомленні замінюється заглушкою.

Викликається першим у /chat — до роутера, до ключа кешу і до моделі: email не йде в модель
і не осідає в ключах кешу, а два запити, що різняться лише адресою, дають один ключ.
"""

import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_PLACEHOLDER = "[email]"


def mask_pii(text: str) -> str:
    return EMAIL.sub(EMAIL_PLACEHOLDER, text)
