"""Shared secret / high-risk-content preflight for local agent clients.

Used by the Compare CLI, the Critique runner, and the service-account reviews
CLI before any packet or prompt text leaves the machine. Patterns are
deliberately conservative: a false positive can be overridden with the
existing --allow-flagged-content flag after manual redaction review; a false
negative sends a live credential to a review panel.

Flags never include matched text, only the pattern that fired.
"""

from __future__ import annotations

import re

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    # Stripe secret/restricted keys use underscores, which the generic sk-
    # pattern above does not match.
    re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"cqa_(?:sa|api)_[A-Za-z0-9_-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+=]{30,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    # Unvalidated JWT shape: three base64url segments, first two JSON objects.
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PRIVATE )?PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{24,}"),
    # Env/assignment-style secrets: the variable name must END with the
    # sensitive keyword (DB_PASSWORD, CLIENT_SECRET, AUTH_TOKEN,
    # OPENAI_API_KEY) so token-count constants like MAX_TOKENS don't fire,
    # and pure numeric values are excluded.
    re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_?KEY)"
        r"\s*[=:]\s*[\"']?(?!\d{8,}[\"']?\s*$)\S{8,}"
    ),
)

HIGH_RISK_PACKET_NAMES = (
    ".env",
    ".pem",
    ".key",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "secrets.",
    "credentials.",
)

# Packet files commonly embed repo paths as "### File: <path>" or
# "Path: <path>" markers; a packet that inlines .env content under such a
# marker is high risk even when no content pattern fires.
_EMBEDDED_PATH_MARKER_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:file|path|source)\s*:\s*(?P<path>\S+)\s*$"
)


def scan_path_name_flags(path) -> list[str]:
    """Flag a packet/prompt file whose own name suggests secret material."""
    lowered = str(path).lower()
    if any(marker in lowered for marker in HIGH_RISK_PACKET_NAMES):
        return [f"high-risk source path: {path}"]
    return []


def scan_text_for_secret_flags(text: str) -> list[str]:
    """Flag likely secrets and embedded high-risk path markers in content."""
    flags: list[str] = []
    value = text or ""
    for pattern in SECRET_PATTERNS:
        if pattern.search(value):
            flags.append(f"matched secret pattern: {pattern.pattern}")
    for match in _EMBEDDED_PATH_MARKER_RE.finditer(value):
        candidate = match.group("path").lower()
        if any(marker in candidate for marker in HIGH_RISK_PACKET_NAMES):
            flags.append(
                f"embedded high-risk source path marker: {match.group('path')}"
            )
    return flags
