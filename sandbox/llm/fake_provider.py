"""FakeProvider агента: детермінована «модель» у процесі, без мережі й ключів.

Курсовий mock-provider (окремий контейнер) не змінюється і агентом не використовується.
Семантика маркерів — як у mock-provider/Program.cs: збій перевіряється раніше за затримку
й «сміття», при __fail_503 разом з __fail_429 перемагає 429. Маркер __fail_first з'явиться
в hw4 разом із fallback-ланцюгом; до того це звичайний текст задачі.
"""

import asyncio
import re
from time import perf_counter

import sandbox.llm.fake_fixtures as fx
from sandbox.llm.types import LLMResult, Usage

_TASK = re.compile(r"<task>(.*?)</task>", re.S)
_CHUNK = re.compile(r'<chunk id="[^"]*" path="([^"]*)" lines="[^"]*" sha256="[0-9a-f]*">\n(.*?)</chunk>', re.S)
_FILE = re.compile(r'<site_file path="([^"]*)" version="(\d+)" sha256="([0-9a-f]*)">\n(.*?)</site_file>', re.S)


def tokens(s: str) -> int:
    # як у mock: ~4 символи = 1 токен
    return 0 if not s else max(1, len(s) // 4)


def _split(messages: list[dict]) -> tuple[str, str]:
    system = user = ""
    for m in messages:
        if m.get("role") == "system":
            system = m.get("content") or ""
        elif m.get("role") == "user":
            user = m.get("content") or ""
    return system, user


class FakeProvider:
    name = "fake"

    def __init__(self, delay_ms: int = 2000):
        self._delay_ms = delay_ms

    async def complete(self, messages: list[dict], model_alias: str, *,
                       timeout_s: float, max_tokens: int | None = None, attempt: int = 1) -> LLMResult:
        t0 = perf_counter()

        def done(**kw) -> LLMResult:
            return LLMResult(latency_ms=int((perf_counter() - t0) * 1000), provider="fake",
                             model_alias=model_alias, **kw)

        if not model_alias.startswith("fake-"):
            # як невідомий аліас у gateway: до моделі запит не дійшов
            return done(ok=False, status=404, content="", error="unknown_alias")

        system, user = _split(messages)
        m = _TASK.search(user)
        task = m.group(1) if m else ""     # маркери й намір — лише з блоку задачі

        fail = None
        if "__fail_503" in task:
            fail = 503
        if "__fail_429" in task:
            fail = 429
        if fail is not None:
            return done(ok=False, status=fail, content="", error=f"http_{fail}")
        if "__delay" in task:
            # неблокуюча пауза: інші запити сервісу в цей час обслуговуються
            await asyncio.sleep(self._delay_ms / 1000)

        lowered = system.lower()
        if "__garbage" in task:
            answer = "..."
        elif "chat json" in lowered:
            files = [(path, content) for path, _version, _sha, content in _FILE.findall(user)]
            answer = fx.chat_json(task, files, system)
        elif "explore json" in lowered:
            answer = fx.explore_json(task, _CHUNK.findall(user), system)
        elif "json plan" in lowered:
            answer = fx.plan_json(task)
        elif "unified diff" in lowered:
            files = [(path, content) for path, _version, _sha, content in _FILE.findall(user)]
            # файли сайту — правка demo-site; фрагменти пакета контексту — .patch для git-проєкту
            answer = fx.edit_diff(task, files) if files else fx.repo_diff(task, _CHUNK.findall(user))
        else:
            answer = fx.NO_FORMAT_TEXT

        usage = Usage(tokens(system) + tokens(user), tokens(answer))
        return done(ok=True, status=200, content=answer, usage=usage)
