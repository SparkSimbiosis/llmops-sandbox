"""Synthetic Engine fixtures. They exercise contracts, not model quality."""
import re
from sandbox.llm.fake_provider import FakeProvider, tokens
from sandbox.llm.types import LLMResult, Usage

async def complete(engine, messages, model, *, max_tokens=None):
    user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    if "__fail_503" in user:
        return LLMResult(ok=False, status=503, content="", model_alias=model, provider="fake", error="http_503")
    if engine == "code_engine":
        return await FakeProvider(delay_ms=0).complete(messages, model, timeout_s=1, max_tokens=max_tokens)
    if "__garbage" in user:
        text = "Synthetic malformed fixture."
    elif engine == "research_engine":
        sources = sorted(set(re.findall(r'(?:path=")([^"\n]+)', user)))
        text = ("# Synthetic research fixture\n\n## Goals\nExercise the research report contract.\n\n"
                "## Sources\n" + ("\n".join("- " + p for p in sources[:5]) or "No supplied source.") +
                "\n\n## Approaches\nCompare two approaches using supplied context only.\n\n"
                "## Synthesis\nThis is a deterministic fixture, not research by a real model.\n\n"
                "## Uncertainties\nNo external facts were verified.\n")
    elif engine == "architecture_engine":
        text = ("# Synthetic ADR fixture\n\n## Context\nLocal preview only.\n\n## Invariants\nNo source writes.\n\n"
                "## Decision\nExplicit preparation before execution.\n\n## Data flow\n"
                "```mermaid\nflowchart LR\nInput --> Prepare --> Check --> Output\n```\n\n"
                "## Risks\nThis fixture does not evaluate architectural quality.\n\n## Consequences\nOperator review remains required.\n")
    elif engine == "critic_engine":
        text = ("# Synthetic review fixture\n\n## Blockers\n- demo-site/header.html:2: supplied change requires manual security review.\n\n"
                "## Suggestions\n- demo-site/header.html:1: verify the proposed diff against the supplied base.\n")
    else:
        text = ("# Синтетична інструкція\n\n## Перевірка\n1. Відкрийте локальний стенд.\n"
                "2. Перегляньте надані файли.\n\n### Приклад використання\n"
                "Приклад очікуваного результату: назва сайту видима в шапці.\n"
                "Це фікстура формату, а не оцінка якості реальної моделі.\n")
    return LLMResult(ok=True, status=200, content=text, provider="fake", model_alias=model,
                     usage=Usage(sum(tokens(m["content"]) for m in messages), tokens(text)))
