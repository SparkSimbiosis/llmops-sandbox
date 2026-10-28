"""Pure passport resolution: no DB, provider, environment files or network."""

from copy import deepcopy

import pytest
import yaml

from sandbox.agent import configs, passports


@pytest.mark.parametrize("project", passports.PROJECT_IDS)
@pytest.mark.parametrize("environment", passports.ENVIRONMENTS)
def test_safety_floor_and_project_permissions(project, environment):
    resolved = passports.passport(project, environment=environment)
    assert resolved["effective_rules"] == passports.GLOBAL_FLOOR
    assert resolved["limits"]["max_retries"] == 0
    assert resolved["permissions"]["actual_repository_apply"] is False
    assert resolved["permissions"]["propose_demo_changes"] == (project == "demo-site" and environment == "lab")
    assert resolved["permissions"]["human_apply_demo"] == (project == "demo-site" and environment == "lab")
    # git project: only a .patch proposal in lab; the human applies it outside the stand
    assert resolved["permissions"]["propose_patches"] == (project == "llm-ops" and environment == "lab")
    assert resolved["permissions"]["prepare_prompts"] and resolved["permissions"]["read_workspace"]


def test_profiles_share_model_but_have_different_budgets():
    quick = passports.passport("llm-ops")
    thorough = passports.passport("llm-ops", "thorough")
    assert quick["model"] == thorough["model"]
    assert quick["limits"] == {"max_tokens": 600, "context_budget": 1000, "max_retries": 0}
    assert thorough["limits"] == {"max_tokens": 1600, "context_budget": 4000, "max_retries": 0}
    assert quick["fingerprint"] != thorough["fingerprint"]


def test_complexity_is_independent_from_profile_and_environment():
    easy = passports.passport("notes-app", "thorough", "production", "easy")
    hard = passports.passport("notes-app", "thorough", "production", "hard")
    assert easy["model"] == hard["model"] and easy["limits"] == hard["limits"]
    assert easy["environment"] == hard["environment"] == "production"
    assert easy["fingerprint"] != hard["fingerprint"]


def test_repeatable_fingerprint_detached_results_and_versioned_roles():
    first = passports.passport("demo-site")
    expected = deepcopy(first)
    first["effective_rules"]["secret_check"] = False
    first["config_stamp"][0]["role_sha256"] = "mutated"
    first["roles"][0]["version"] = "mutated"
    again = passports.passport("demo-site")
    assert again == expected
    for role in again["roles"]:
        assert role["version"] == configs.ROLES[role["name"]]["sha256"]
        assert role["version_scheme"] == "sha256"
    assert len(again["fingerprint"]) == 64


@pytest.mark.parametrize("kwargs", [{"project": "unknown"}, {"project": "llm-ops", "profile": "lab"},
                                   {"project": "llm-ops", "environment": "quick"},
                                   {"project": "llm-ops", "complexity": "complex"}])
def test_unknown_resolution_fails_closed(kwargs):
    with pytest.raises(passports.PassportConfigError):
        passports.passport(**kwargs)


@pytest.mark.parametrize("mutation", ["unknown", "floor", "retries", "permission", "patches", "environment", "model", "role",
                                      "limits", "type"])
def test_invalid_or_contradictory_config_fails_closed(monkeypatch, tmp_path, mutation):
    config = yaml.safe_load(passports.CONFIG_FILE.read_text(encoding="utf-8"))
    if mutation == "unknown":
        config["new_rule"] = True
    elif mutation == "floor":
        config["global_floor"]["secret_check"] = False
    elif mutation == "retries":
        config["global_floor"]["max_retries_after_uncertain_outcome"] = 1
    elif mutation == "permission":
        config["projects"]["llm-ops"]["human_apply_demo"] = True
    elif mutation == "patches":
        config["projects"]["notes-app"]["propose_patches"] = True
    elif mutation == "environment":
        config["environments"]["production"]["allow_changes"] = True
    elif mutation == "model":
        config["execution_profiles"]["thorough"]["model_class"] = "reasoning"
    elif mutation == "role":
        config["projects"]["demo-site"]["roles"] = ["invented"]
    elif mutation == "limits":
        config["execution_profiles"]["quick"]["context_budget"] = 4001
    elif mutation == "type":
        config["global_floor"]["secret_check"] = 1
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(passports, "CONFIG_FILE", path)
    with pytest.raises(passports.PassportConfigError):
        passports.passport("notes-app")


def test_duplicate_yaml_keys_fail_closed(monkeypatch, tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(passports.CONFIG_FILE.read_text(encoding="utf-8") + "\nversion: 1\n", encoding="utf-8")
    monkeypatch.setattr(passports, "CONFIG_FILE", path)
    with pytest.raises(passports.PassportConfigError, match="duplicate"):
        passports.passport("demo-site")


def test_config_change_and_role_stamp_change_affect_identity(monkeypatch, tmp_path):
    before = passports.passport("notes-app")
    config = yaml.safe_load(passports.CONFIG_FILE.read_text(encoding="utf-8"))
    config["projects"]["notes-app"]["title"] += " updated"
    path = tmp_path / "projects.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(passports, "CONFIG_FILE", path)
    after = passports.passport("notes-app")
    assert before["fingerprint"] != after["fingerprint"]
    role = deepcopy(configs.ROLES["explorer"])
    role["sha256"] = "0" * 64
    monkeypatch.setitem(configs.ROLES, "explorer", role)
    assert passports.passport("notes-app")["fingerprint"] != after["fingerprint"]
