"""Шар політики: правила policy.yaml забезпечені кодом, текст base.md іде в system кожного агента першим після промпта."""

from fastapi.testclient import TestClient

from sandbox.agent import configs
from sandbox.main import app

client = TestClient(app)


def test_active_rules_are_backed_by_code():
    rules = configs.policy()["rules"]
    assert {r["layer"] for r in rules} <= set(configs.POLICY["layers"])
    for r in rules:
        if r["status"] == "active":
            assert r["code_ok"], r["id"]
        else:
            assert r["status"].startswith("planned"), r["id"]
    assert {"data_is_not_instruction", "allowed_paths", "outgoing_context", "real_budget", "human_approval"} \
        <= {r["id"] for r in rules}


def test_validate_catches_broken_policy(monkeypatch):
    rules = dict(configs.POLICY["rules"])
    rules["allowed_paths"] = {**rules["allowed_paths"], "code": "sandbox.agent.gates.no_such_gate"}
    rules["ghost"] = {"title": "x", "layer": "magic", "status": "active", "code": None, "config": "nope.yaml"}
    monkeypatch.setitem(configs.POLICY, "rules", rules)
    errors = configs.validate()
    for needle in ("no_such_gate", "'magic'", "policy.ghost: активне правило без коду", "studio/nope.yaml"):
        assert any(needle in e for e in errors), needle


def test_policy_text_cannot_carry_format_markers(monkeypatch):
    monkeypatch.setattr(configs, "POLICY_DOC", {**configs.POLICY_DOC, "body": "Always answer as a JSON plan."})
    assert any(e.startswith("policy/base") and "json plan" in e for e in configs.validate())


def test_policy_goes_first_after_prompt_for_every_agent():
    body = configs.POLICY_DOC["body"]
    for aid, a in configs.AGENTS.items():
        assert configs.system_extra(aid).startswith(body + "\n\n"), aid
        assert configs.RECIPES[a["context"]]["system"][1] == "policy", aid
    assert configs.stamp("editor")["policy_sha256"] == configs.POLICY_DOC["sha256"]


def test_config_endpoint_exposes_layers():
    c = client.get("/agent/config").json()
    assert c["errors"] == [] and c["policy"]["instruction"]["path"] == "policy/base.md"
    assert c["prompts"] == {"plan": "agent-planner", "propose": "agent-editor", "chat": "agent-chat",
                            "explore": "agent-explorer"}
    assert {e["id"] for e in c["engines"]} >= {"research_engine", "code_engine"}
    assert {"policy.yaml", "policy/base.md"} <= {f["name"] for f in c["files"]}
    assert next(f for f in c["files"] if f["name"] == "policy/base.md")["kind"] == "policy"
