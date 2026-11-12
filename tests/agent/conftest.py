"""Спільна фікстура тестів агента: конвеєр на репозиторії в пам'яті й FakeProvider без затримки."""

import pytest

from sandbox.agent import pipeline
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.provider_router import LLMRouter
from tests.agent.memory_repo import MemoryAgentRepo


@pytest.fixture
def mem(monkeypatch):
    repo = MemoryAgentRepo()
    monkeypatch.setattr(pipeline, "repo", repo)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(repo))
    monkeypatch.setattr(pipeline, "llm", LLMRouter(FakeProvider(delay_ms=0)))
    return repo


@pytest.fixture(autouse=True)
def isolated_execution_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_EXECUTION_DIR", str(tmp_path / "executions"))
