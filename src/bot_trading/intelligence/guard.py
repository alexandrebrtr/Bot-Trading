"""Protection du contenu externe avant tout envoi à un LLM.

Règles :
1. Le contenu externe (discours, communiqués, actualités, 8-K) est une **donnée**.
   Il est nettoyé, tronqué, encodé en JSON et placé dans un bloc marqué
   « données non fiables » ; il n'est jamais concaténé aux instructions.
2. Les tentatives d'injection détectées sont signalées et le texte suspect est
   neutralisé (remplacé par un marqueur), pas exécuté.
3. Aucun secret ne quitte la machine : les valeurs des variables sensibles et
   les motifs de clés sont masqués, et un envoi qui en contiendrait encore est
   refusé.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

SECRET_ENV_VARS = (
    "OPENAI_API_KEY",
    "CAPITAL_API_KEY",
    "CAPITAL_IDENTIFIER",
    "CAPITAL_API_PASSWORD",
    "FRED_API_KEY",
    "BLS_API_KEY",
    "BEA_API_KEY",
    "SEC_USER_AGENT",
    "TELEGRAM_BOT_TOKEN",
)

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token|cst|x-security-token)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),  # longues chaînes opaques (clés)
]

INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)",
        r"disregard (the )?(previous|prior|above|system)",
        r"forget (all |your )?(previous )?instructions",
        r"\byou are now\b",
        r"\bact as\b",
        r"system prompt",
        r"developer (message|mode)",
        r"\bnew instructions?\b",
        r"<\|?(system|im_start|im_end|endoftext)\|?>",
        r"\b(place|send|execute|submit) (an? )?(buy|sell|market|limit)? ?orders?\b",
        r"\b(api[_ -]?key|password|credentials?)\b",
        r"\bset (the )?(tone|score|bias) to\b",
        r"respond (only )?with",
    )
]

_INVISIBLE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏ -‮⁠-⁤﻿]")


@dataclass(frozen=True)
class SanitizedText:
    text: str
    truncated: bool
    injection_flags: list[str] = field(default_factory=list)
    redactions: int = 0

    @property
    def suspicious(self) -> bool:
        return bool(self.injection_flags)


def secret_values() -> list[str]:
    return [v for k in SECRET_ENV_VARS if (v := os.environ.get(k)) and len(v) >= 6]


def redact_secrets(text: str) -> tuple[str, int]:
    n = 0
    for v in secret_values():
        if v in text:
            n += text.count(v)
            text = text.replace(v, "[SECRET MASQUÉ]")
    for p in SECRET_PATTERNS:
        text, k = p.subn("[SECRET MASQUÉ]", text)
        n += k
    return text, n


def sanitize_external_text(text: str, max_chars: int = 6000) -> SanitizedText:
    t = unicodedata.normalize("NFKC", text)
    t = _INVISIBLE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t).strip()
    flags: list[str] = []
    for p in INJECTION_PATTERNS:
        if p.search(t):
            flags.append(p.pattern)
            t = p.sub("[INSTRUCTION NEUTRALISÉE]", t)
    t, n = redact_secrets(t)
    truncated = len(t) > max_chars
    return SanitizedText(t[:max_chars], truncated, flags, n)


def assert_no_secret(payload: str) -> None:
    """Dernier contrôle avant envoi : lève si une valeur secrète est présente."""
    for v in secret_values():
        if v in payload:
            raise PermissionError("Envoi refusé : le contenu contient un secret de l'environnement")
    if SECRET_PATTERNS[0].search(payload):
        raise PermissionError("Envoi refusé : motif de clé API détecté")
