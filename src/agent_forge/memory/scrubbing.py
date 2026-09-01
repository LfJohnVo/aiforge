"""PII scrubbing, applied before anything persistent is written.

Runs on every memory layer: short term, long term, episodic and the semantic cache. The
rule is one-directional -- text goes into memory scrubbed, and there is no unscrub.

The built-in engine is deterministic regex plus a checksum where the format has one
(Luhn for cards, mod-97 for IBAN, the check letter for RFC/CURP/NIF). It is always
active. Presidio (extra ``guardrails``) runs *after* it when installed and can only add
findings, never remove them -- same asymmetry as the DLP engine in ADR-006.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

PLACEHOLDER = "[{kind}]"


@dataclass(frozen=True, slots=True)
class Finding:
    """One detected span."""

    kind: str
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """Scrubbed text plus what was removed, for metrics and the ledger."""

    text: str
    findings: tuple[Finding, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings

    def kinds(self) -> list[str]:
        return sorted({f.kind for f in self.findings})


def _luhn(digits: str) -> bool:
    numbers = [int(d) for d in digits if d.isdigit()]
    if len(numbers) < 12:
        return False
    checksum = 0
    parity = len(numbers) % 2
    for index, digit in enumerate(numbers):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _iban_valid(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    if len(compact) < 15 or len(compact) > 34:
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    if not digits.isdigit():
        return False
    return int(digits) % 97 == 1


# Spain: NIF/NIE check letter.
_NIF_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _nif_valid(value: str) -> bool:
    text = value.upper().replace("-", "")
    if re.fullmatch(r"\d{8}[A-Z]", text):
        return _NIF_LETTERS[int(text[:8]) % 23] == text[8]
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", text):
        prefix = {"X": "0", "Y": "1", "Z": "2"}[text[0]]
        return _NIF_LETTERS[int(prefix + text[1:8]) % 23] == text[8]
    return False


# Each rule: kind, pattern, optional validator. Validators exist to cut false positives
# on formats that look like ordinary numbers -- an order id should not be scrubbed as a
# credit card just because it has 16 digits.
_RULES: tuple[tuple[str, re.Pattern[str], Any], ...] = (
    ("EMAIL", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), None),
    (
        # The digit-then-separator form must not swallow the trailing separator, or the
        # replacement eats the following space and words run together.
        "CREDIT_CARD",
        re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
        lambda m: _luhn(m.group(0)),
    ),
    (
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b"),
        lambda m: _iban_valid(m.group(0)),
    ),
    # Mexico: CURP (18) before RFC (12-13) so the longer one wins.
    ("CURP", re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b"), None),
    ("RFC", re.compile(r"\b[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}\b"), None),
    ("NIF", re.compile(r"\b(?:[XYZ]?\d{7,8}-?[A-Z])\b"), lambda m: _nif_valid(m.group(0))),
    # Before PHONE: an SSN is also a 9-digit number, and the more specific format
    # must win or the ledger records the wrong kind of finding.
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
    (
        # Deliberately permissive on grouping (ES, MX and international forms all differ)
        # and strict on digit count, which is what keeps order ids and dates out.
        "PHONE",
        re.compile(
            r"(?<![\w])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?"
            r"\d{2,4}(?:[ .-]?\d{2,4}){2,4}(?![\w])"
        ),
        lambda m: 9 <= sum(c.isdigit() for c in m.group(0)) <= 15,
    ),
    (
        "IP",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        lambda m: all(int(p) <= 255 for p in m.group(0).split(".")),
    ),
)


@dataclass(slots=True)
class Scrubber:
    """Removes PII from text before it is persisted."""

    enabled: bool = True
    # Kinds to leave alone. Some areas legitimately store IPs (SOC) or emails (support).
    allow_kinds: frozenset[str] = field(default_factory=frozenset)
    use_presidio: bool = True
    language: str = "es"

    def scrub(self, text: str) -> ScrubResult:
        """Replace every detected span with a typed placeholder."""
        if not self.enabled or not text:
            return ScrubResult(text=text)

        findings = self._detect(text)
        if not findings:
            return ScrubResult(text=text)

        # Replace right to left so earlier offsets stay valid.
        out = text
        for finding in sorted(findings, key=lambda f: f.start, reverse=True):
            out = out[: finding.start] + PLACEHOLDER.format(kind=finding.kind) + out[finding.end :]
        return ScrubResult(text=out, findings=tuple(sorted(findings, key=lambda f: f.start)))

    def scrub_all(self, values: Iterable[str]) -> list[str]:
        return [self.scrub(value).text for value in values]

    # ------------------------------------------------------------- detection

    def _detect(self, text: str) -> list[Finding]:
        found = list(self._detect_builtin(text))
        found.extend(self._detect_presidio(text))
        return _drop_overlaps(found)

    def _detect_builtin(self, text: str) -> Iterable[Finding]:
        for kind, pattern, validator in _RULES:
            if kind in self.allow_kinds:
                continue
            for match in pattern.finditer(text):
                if validator is not None and not validator(match):
                    continue
                yield Finding(kind=kind, start=match.start(), end=match.end(), text=match.group(0))

    def _detect_presidio(self, text: str) -> list[Finding]:
        """Optional reinforcement. Its absence must never weaken the result."""
        if not self.use_presidio:
            return []
        analyzer = _presidio_analyzer(self.language)
        if analyzer is None:
            return []
        try:
            results = analyzer.analyze(text=text, language=self.language)
        except Exception as exc:
            log.warning("scrubbing.presidio_failed", detail=type(exc).__name__)
            return []
        return [
            Finding(
                kind=str(r.entity_type),
                start=int(r.start),
                end=int(r.end),
                text=text[r.start : r.end],
            )
            for r in results
            if str(r.entity_type) not in self.allow_kinds and float(r.score) >= 0.6
        ]


@lru_cache(maxsize=4)
def _presidio_analyzer(language: str) -> Any | None:
    """Build the Presidio analyzer once, or report it unavailable once."""
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError:
        log.debug("scrubbing.presidio_not_installed", detail="built-in rules only")
        return None
    try:
        return AnalyzerEngine(supported_languages=[language])
    except Exception as exc:
        log.warning("scrubbing.presidio_unavailable", detail=type(exc).__name__)
        return None


def _drop_overlaps(findings: Sequence[Finding]) -> list[Finding]:
    """Keep the longest span when two rules match the same text.

    Without this, CURP and RFC both fire on the same string and the replacement produces
    nonsense.
    """
    ordered = sorted(findings, key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Finding] = []
    for finding in ordered:
        if any(finding.start < k.end and k.start < finding.end for k in kept):
            continue
        kept.append(finding)
    return kept
