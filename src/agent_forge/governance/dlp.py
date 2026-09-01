"""DLP and prompt firewall, on the way in and on the way out.

Two directions, two different fears. Inbound: an injected instruction, a jailbreak
persona, a secret the user pasted that must not reach the model. Outbound: a secret that
was already in the tenant's own corpus, or a stack trace that tells an attacker how the
cell is built.

PII detection is **not** reimplemented here. ``memory/scrubbing.py`` already has the
checksum-backed detectors and the optional Presidio layer; this engine calls it. Two PII
detectors would be two sets of rules to keep in step, and the day they disagree is the day
one of them is wrong.

The rest -- injection, jailbreak, secrets, output hygiene -- is data in
``configs/policies/dlp_rules.yaml``. Adding a rule is editing a file, and every rule
carries a stable id so a block can be explained in the ledger and to the user.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml

from agent_forge.memory.scrubbing import Scrubber
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)

__all__ = [
    "DlpEngine",
    "DlpMatch",
    "DlpResult",
    "DlpRule",
    "Severity",
    "load_rules",
]

Direction = Literal["input", "output"]
Action = Literal["block", "redact", "flag"]


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class DlpRule:
    """One compiled rule."""

    id: str
    description: str
    pattern: re.Pattern[str]
    severity: Severity
    action: Action
    direction: str  # input | output | both

    def applies_to(self, direction: Direction) -> bool:
        return self.direction in {direction, "both"}


@dataclass(frozen=True, slots=True)
class DlpMatch:
    """What a rule found. The text itself is never carried: a match on a secret would
    otherwise put that secret in the log that records the match."""

    rule_id: str
    severity: Severity
    action: Action
    count: int


@dataclass(frozen=True, slots=True)
class DlpResult:
    """Outcome of scanning one piece of text."""

    text: str
    matches: tuple[DlpMatch, ...] = ()
    blocked: bool = False
    pii_kinds: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.matches and not self.pii_kinds

    @property
    def redacted(self) -> bool:
        return any(m.action == "redact" for m in self.matches) or bool(self.pii_kinds)

    def reasons(self) -> tuple[str, ...]:
        """One line per rule that fired, for the ledger and the refusal message."""
        return tuple(
            f"{m.rule_id} ({m.severity}, {m.action})" for m in self.matches if m.action != "flag"
        ) + tuple(f"pii:{kind}" for kind in self.pii_kinds)

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(m.rule_id for m in self.matches)


def load_rules(path: str | Path) -> tuple[DlpRule, ...]:
    """Read and compile the rule file. A malformed rule is fatal, not skipped.

    Skipping it would leave the cell running with a firewall quietly missing a rule
    somebody believed was in force.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules: list[DlpRule] = []
    for index, entry in enumerate(raw.get("rules") or ()):
        try:
            rules.append(
                DlpRule(
                    id=str(entry["id"]),
                    description=str(entry.get("description", "")),
                    pattern=re.compile(entry["pattern"], re.IGNORECASE | re.MULTILINE),
                    severity=Severity(entry.get("severity", "medium")),
                    action=entry.get("action", "flag"),
                    direction=entry.get("direction", "both"),
                )
            )
        except (KeyError, ValueError, re.error) as exc:
            raise ValueError(f"DLP rule #{index} in {path} is invalid: {exc}") from exc
    if not rules:
        raise ValueError(f"{path} defines no DLP rules")
    log.info("dlp.rules_loaded", path=str(path), count=len(rules))
    return tuple(rules)


@dataclass(slots=True)
class DlpEngine:
    """Scans text in both directions and applies each rule's action."""

    rules: tuple[DlpRule, ...] = ()
    scrubber: Scrubber | None = None
    # PII on the way in is redacted, not blocked: a user asking about their own invoice
    # is the normal case, and refusing it would make the cell useless. What must not
    # happen is that PII reaches an external model or a persistent store, and that is
    # decided by the model policy and by memory scrubbing, not here.
    redact_pii_inbound: bool = True
    redact_pii_outbound: bool = False
    _by_direction: dict[str, tuple[DlpRule, ...]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._by_direction = {
            "input": tuple(r for r in self.rules if r.applies_to("input")),
            "output": tuple(r for r in self.rules if r.applies_to("output")),
        }

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> DlpEngine:
        return cls(rules=load_rules(path), scrubber=Scrubber(), **kwargs)

    @classmethod
    def disabled(cls) -> DlpEngine:
        """A no-op engine, for a profile that turns DLP off."""
        return cls(rules=(), scrubber=None)

    def scan(self, text: str, direction: Direction = "input", *, tenant_id: str = "") -> DlpResult:
        """Apply every rule for this direction, then PII.

        Order matters: a blocking rule short-circuits, because there is no point
        redacting text that is not going to be processed.
        """
        if not text:
            return DlpResult(text=text)

        matches: list[DlpMatch] = []
        working = text
        for rule in self._by_direction.get(direction, ()):
            found = rule.pattern.findall(working)
            if not found:
                continue
            matches.append(
                DlpMatch(
                    rule_id=rule.id,
                    severity=rule.severity,
                    action=rule.action,
                    count=len(found),
                )
            )
            if rule.action == "block":
                get_metrics().dlp_findings.labels(
                    tenant=tenant_id or "_",
                    rule=rule.id,
                    direction=direction,
                    action="block",
                ).inc()
                log.warning(
                    "dlp.blocked", rule=rule.id, direction=direction, severity=str(rule.severity)
                )
                return DlpResult(text=text, matches=tuple(matches), blocked=True)
            if rule.action == "redact":
                working = rule.pattern.sub(f"[{rule.id}]", working)

        pii_kinds: tuple[str, ...] = ()
        should_redact = (
            self.redact_pii_inbound if direction == "input" else self.redact_pii_outbound
        )
        if self.scrubber is not None:
            result = self.scrubber.scrub(working)
            pii_kinds = tuple(result.kinds())
            if pii_kinds and should_redact:
                working = result.text

        if matches or pii_kinds:
            metrics = get_metrics()
            for match in matches:
                metrics.dlp_findings.labels(
                    tenant=tenant_id or "_",
                    rule=match.rule_id,
                    direction=direction,
                    action=match.action,
                ).inc()
            for kind in pii_kinds:
                metrics.dlp_findings.labels(
                    tenant=tenant_id or "_",
                    rule=f"pii:{kind}",
                    direction=direction,
                    action="redact",
                ).inc()
            log.info(
                "dlp.scanned",
                direction=direction,
                rules=[m.rule_id for m in matches],
                pii=list(pii_kinds),
            )
        return DlpResult(text=working, matches=tuple(matches), pii_kinds=pii_kinds)

    def scan_all(
        self, texts: Iterable[str], direction: Direction = "input", *, tenant_id: str = ""
    ) -> list[DlpResult]:
        return [self.scan(text, direction, tenant_id=tenant_id) for text in texts]

    @property
    def enabled(self) -> bool:
        return bool(self.rules) or self.scrubber is not None

    def rule_ids(self) -> Sequence[str]:
        return [rule.id for rule in self.rules]
