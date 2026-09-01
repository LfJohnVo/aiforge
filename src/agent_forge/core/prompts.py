"""Versioned prompt registry.

Prompts are procedural memory (RF-04) and are treated like code: versioned files, manual
promotion, no hot editing. The registry loads ``configs/prompts/<id>/v<N>.md``, each with
YAML frontmatter declaring the variables it needs, and validates at load time that a
prompt cannot reference a variable the caller does not provide -- a broken template
should fail at startup, not halfway through a customer's request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from agent_forge.core.errors import AgentForgeError

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_VERSION_FILE = re.compile(r"^v(\d+)\.md$")


class PromptError(AgentForgeError):
    """A prompt is missing, malformed, or rendered with the wrong variables."""

    code = "prompt_invalid"


@dataclass(frozen=True, slots=True)
class Prompt:
    """One version of one prompt."""

    id: str
    version: int
    template: str
    inputs: tuple[str, ...]
    owner: str = ""
    evals: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """Stable reference recorded in traces and in the evidence ledger."""
        return f"{self.id}@v{self.version}"


class PromptRegistry:
    """All prompts on disk, addressable by id and version."""

    def __init__(self, prompts: dict[str, dict[int, Prompt]]) -> None:
        self._prompts = prompts
        self._env = Environment(  # noqa: S701 - prompts are Markdown, not HTML
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    @classmethod
    def from_directory(cls, root: str | Path) -> PromptRegistry:
        """Load every ``<id>/v<N>.md`` under ``root``."""
        base = Path(root)
        if not base.is_dir():
            raise PromptError("prompt directory not found", path=str(base))

        prompts: dict[str, dict[int, Prompt]] = {}
        for prompt_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for file in sorted(prompt_dir.iterdir()):
                match = _VERSION_FILE.match(file.name)
                if match is None:
                    continue
                prompt = _load_prompt(file, prompt_dir.name, int(match.group(1)))
                prompts.setdefault(prompt.id, {})[prompt.version] = prompt
        return cls(prompts)

    def ids(self) -> list[str]:
        return sorted(self._prompts)

    def versions(self, prompt_id: str) -> list[int]:
        return sorted(self._prompts.get(prompt_id, {}))

    def get(self, prompt_id: str, version: int | None = None) -> Prompt:
        """Fetch a prompt; ``None`` means the highest version on disk."""
        versions = self._prompts.get(prompt_id)
        if not versions:
            raise PromptError("unknown prompt", prompt=prompt_id, known=self.ids())
        if version is None:
            version = max(versions)
        prompt = versions.get(version)
        if prompt is None:
            raise PromptError(
                "unknown prompt version",
                prompt=prompt_id,
                version=version,
                known=sorted(versions),
            )
        return prompt

    def render(self, prompt_id: str, /, version: int | None = None, **values: Any) -> str:
        """Render a prompt, refusing to guess at missing variables."""
        prompt = self.get(prompt_id, version)
        missing = [name for name in prompt.inputs if name not in values]
        if missing:
            raise PromptError(
                "prompt is missing declared inputs",
                prompt=prompt.ref,
                missing=missing,
            )
        try:
            return self._env.from_string(prompt.template).render(**values).strip()
        except TemplateError as exc:
            raise PromptError(
                "prompt failed to render", prompt=prompt.ref, detail=str(exc)
            ) from exc


def _load_prompt(path: Path, prompt_id: str, version: int) -> Prompt:
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise PromptError("prompt has no YAML frontmatter declaring its inputs", path=str(path))
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise PromptError(
            "prompt frontmatter is not valid YAML", path=str(path), detail=str(exc)
        ) from exc
    if not isinstance(meta, dict):
        raise PromptError("prompt frontmatter must be a mapping", path=str(path))

    declared_id = str(meta.get("id", prompt_id))
    if declared_id != prompt_id:
        raise PromptError(
            "prompt id does not match its directory",
            path=str(path),
            declared=declared_id,
            directory=prompt_id,
        )
    declared_version = int(meta.get("version", version))
    if declared_version != version:
        raise PromptError(
            "prompt version does not match its filename",
            path=str(path),
            declared=declared_version,
            filename=version,
        )

    return Prompt(
        id=prompt_id,
        version=version,
        template=raw[match.end() :],
        inputs=tuple(meta.get("inputs") or ()),
        owner=str(meta.get("owner", "")),
        evals=tuple(meta.get("evals") or ()),
    )
