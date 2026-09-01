"""Load, expand and validate an ``agent.profile.yaml``.

Two rules that matter more than they look:

* ``${VAR}`` is resolved from the environment. A missing variable is an **error**, never
  an empty string: a profile that silently points at ``""`` produces an agent that fails
  in confusing ways hours later.
* Validation errors are reported all at once, with the YAML path of each problem, so a
  misconfigured profile takes one round trip to fix instead of five.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_forge.core.errors import ProfileError
from agent_forge.profile.models import AgentProfile

# ${VAR} and ${VAR:-default}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any, env: dict[str, str], *, path: str = "") -> Any:
    """Recursively substitute ``${VAR}`` placeholders in strings.

    A whole-string placeholder for an unset variable with no default raises; a partial
    one does too. Both cases produce a message naming the YAML path and the variable.
    """
    if isinstance(value, dict):
        return {
            k: expand_env(v, env, path=f"{path}.{k}" if path else str(k)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [expand_env(v, env, path=f"{path}[{i}]") for i, v in enumerate(value)]
    if not isinstance(value, str):
        return value

    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        missing.append(name)
        return ""

    result = _PLACEHOLDER.sub(replace, value)
    if missing:
        raise ProfileError(
            "profile references environment variables that are not set",
            path=path or "<root>",
            variables=sorted(set(missing)),
        )
    return result


def load_profile(
    path: str | Path,
    *,
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> AgentProfile:
    """Read a profile from disk and return the validated model.

    ``overrides`` is a shallow-merged mapping applied after expansion; instances use it
    to override a handful of keys without duplicating the whole file.
    """
    profile_path = Path(path)
    if not profile_path.is_file():
        raise ProfileError("profile file not found", path=str(profile_path))

    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(
            "profile is not valid YAML", path=str(profile_path), detail=str(exc)
        ) from exc

    if not isinstance(raw, dict):
        raise ProfileError("profile must be a YAML mapping", path=str(profile_path))

    data = expand_env(raw, dict(os.environ) if env is None else env)
    if overrides:
        data = _deep_merge(data, overrides)

    return parse_profile(data, source=str(profile_path))


def parse_profile(data: dict[str, Any], *, source: str = "<memory>") -> AgentProfile:
    """Validate an already-expanded mapping, reporting every problem at once."""
    try:
        return AgentProfile.model_validate(data)
    except ValidationError as exc:
        problems = [
            {
                "at": ".".join(str(part) for part in error["loc"]) or "<root>",
                "problem": error["msg"],
            }
            for error in exc.errors()
        ]
        raise ProfileError(
            f"profile is invalid ({len(problems)} problem(s))",
            source=source,
            problems=problems,
        ) from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into ``base``; nested mappings merge, everything else wins."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def format_profile_error(error: ProfileError) -> str:
    """Render a profile error the way it should appear on a failed startup."""
    lines = [f"Perfil invalido: {error.message}"]
    source = error.context.get("source") or error.context.get("path")
    if source:
        lines.append(f"  archivo: {source}")
    problems = error.context.get("problems")
    if isinstance(problems, list):
        lines.extend(f"  - {p['at']}: {p['problem']}" for p in problems)
    variables = error.context.get("variables")
    if isinstance(variables, list):
        lines.append(f"  variables de entorno sin definir: {', '.join(variables)}")
        lines.append("  revisa tu .env contra .env.example")
    return "\n".join(lines)
