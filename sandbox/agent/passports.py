"""Deterministic project passports; declarations are not runtime permission grants.

Environment, execution profile and task complexity are independent axes. Every
resolution validates the whole local configuration and returns a detached JSON
snapshot. The caller must enforce the resolved limits and permissions.
"""

from copy import deepcopy
from pathlib import Path

import yaml

from sandbox.agent import configs, routing
from sandbox.core.hashing import canonical_json, sha256_hex

CONFIG_FILE = Path(__file__).resolve().parent / "studio" / "projects.yaml"
PROJECT_IDS = ("demo-site", "notes-app", "llm-ops")
ENVIRONMENTS = ("lab", "staging", "production")
PROFILES = ("quick", "thorough")
COMPLEXITIES = ("easy", "medium", "hard")
GLOBAL_FLOOR = {"secret_check": True, "human_approval_for_apply": True,
                "actual_repository_apply": False, "max_retries_after_uncertain_outcome": 0}


class PassportConfigError(ValueError):
    """Invalid, unknown or contradictory passport configuration: fail closed."""


def _keys(value, required, where, errors):
    if not isinstance(value, dict) or set(value) != set(required):
        errors.append(f"{where}: unexpected or missing keys")
        return False
    return True


def validate_config(config) -> list[str]:
    errors = []
    if not _keys(config, ("version", "global_floor", "execution_profiles", "environments", "projects"), "root", errors):
        return errors
    if type(config["version"]) is not int or config["version"] != 1:
        errors.append("version: unsupported")
    floor = config["global_floor"]
    if not _keys(floor, GLOBAL_FLOOR, "global_floor", errors) or any(
            type(floor.get(k)) is not type(v) or floor.get(k) != v for k, v in GLOBAL_FLOOR.items()):
        errors.append("global_floor: safety invariants cannot be weakened")
    profiles = config["execution_profiles"]
    if _keys(profiles, PROFILES, "execution_profiles", errors):
        for name, limits in profiles.items():
            if not _keys(limits, ("max_tokens", "context_budget", "model_class"), f"profile.{name}", errors):
                continue
            for field, maximum in (("max_tokens", 1600), ("context_budget", 4000)):
                if type(limits[field]) is not int or not 1 <= limits[field] <= maximum:
                    errors.append(f"profile.{name}.{field}: out of range")
            if limits["model_class"] != "fast":
                errors.append(f"profile.{name}: only the shared fast model is allowed")
        if all(isinstance(profiles.get(p), dict) for p in PROFILES):
            for field in ("max_tokens", "context_budget"):
                quick, thorough = (profiles[p].get(field) for p in PROFILES)
                if type(quick) is int and type(thorough) is int and quick > thorough:
                    errors.append(f"profiles.{field}: quick exceeds thorough")
    environments = config["environments"]
    if _keys(environments, ENVIRONMENTS, "environments", errors):
        for name, env in environments.items():
            if _keys(env, ("allow_changes",), f"environment.{name}", errors):
                expected = name == "lab"
                if type(env["allow_changes"]) is not bool or env["allow_changes"] != expected:
                    errors.append(f"environment.{name}: contradictory change permissions")
    projects = config["projects"]
    if _keys(projects, PROJECT_IDS, "projects", errors):
        for name, project in projects.items():
            if not _keys(project, ("title", "kind", "roles", "propose_demo_changes", "human_apply_demo", "propose_patches"),
                         f"project.{name}", errors):
                continue
            if not isinstance(project["title"], str) or not project["title"].strip():
                errors.append(f"project.{name}.title: required")
            expected_kind = {"demo-site": "demo", "notes-app": "sample", "llm-ops": "mount"}[name]
            if project["kind"] != expected_kind:
                errors.append(f"project.{name}.kind: inconsistent")
            roles = project["roles"]
            if not isinstance(roles, list) or not roles or any(not isinstance(r, str) or r not in configs.ROLES for r in roles):
                errors.append(f"project.{name}.roles: unknown or empty")
            elif len(set(roles)) != len(roles):
                errors.append(f"project.{name}.roles: duplicate")
            # demo-site змінюється в БД стенда; git-проєкт — лише пропозиція .patch, застосовує людина поза стендом
            for permission, owner in (("propose_demo_changes", "demo-site"), ("human_apply_demo", "demo-site"),
                                      ("propose_patches", "llm-ops")):
                expected = name == owner
                if type(project[permission]) is not bool or project[permission] != expected:
                    errors.append(f"project.{name}.{permission}: contradictory")
    return errors


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in mapping:
                raise PassportConfigError("duplicate passport configuration key")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise PassportConfigError("invalid passport configuration key") from exc
    return mapping


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _load() -> dict:
    try:
        config = yaml.load(CONFIG_FILE.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PassportConfigError("passport configuration unavailable or invalid") from exc
    errors = validate_config(config)
    if errors:
        raise PassportConfigError("; ".join(errors))
    return config


def passport(project: str, profile: str = "quick", environment: str = "lab", complexity: str = "easy") -> dict:
    config = _load()
    if project not in PROJECT_IDS or profile not in PROFILES or environment not in ENVIRONMENTS or complexity not in COMPLEXITIES:
        raise PassportConfigError("unknown project, execution profile, environment or task complexity")
    entry = config["projects"][project]
    execution = config["execution_profiles"][profile]
    can_change = config["environments"][environment]["allow_changes"]
    roles = [{"name": name, "path": configs.ROLES[name]["path"],
              "version": configs.ROLES[name]["sha256"], "version_scheme": "sha256"} for name in entry["roles"]]
    stamps = [configs.stamp(aid) for aid, agent in sorted(configs.AGENTS.items()) if agent["role"] in entry["roles"]]
    result = {"version": 1, "project": project, "title": entry["title"], "kind": entry["kind"],
              "profile": profile, "environment": environment, "complexity": complexity,
              "model": {"class": execution["model_class"], "real_alias": routing.alias(execution["model_class"], "real")},
              "permissions": {"read_workspace": True, "prepare_prompts": True,
                              "propose_demo_changes": can_change and entry["propose_demo_changes"],
                              "human_apply_demo": can_change and entry["human_apply_demo"],
                              "propose_patches": can_change and entry["propose_patches"],
                              "actual_repository_apply": False},
              "limits": {"max_tokens": execution["max_tokens"], "context_budget": execution["context_budget"],
                         "max_retries": 0},
              "effective_rules": dict(GLOBAL_FLOOR), "roles": roles, "config_stamp": stamps,
              "enforcement": "declaration_requires_runtime_enforcement"}
    result["fingerprint"] = sha256_hex(canonical_json({"configuration": config, "effective": result}))
    return deepcopy(result)
