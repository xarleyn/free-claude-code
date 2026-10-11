"""Client attribution from reliable signals only.

A request's ``client_type`` is used for analytics attribution, so it must never be
guessed. Only headers that the known integrations actually send are honoured; anything
else stays ``unknown``. Guessing would corrupt the analytics breakdown, and a wrong
label is worse than an honest ``unknown``.
"""

from collections.abc import Mapping

from free_claude_code.application.analytics.models import ClientType

#: Header fragments that unambiguously identify an integration. Each entry is
#: matched case-insensitively against the ``user-agent`` and ``x-app`` headers.
_SIGNALS: tuple[tuple[str, ClientType], ...] = (
    ("claude-code", ClientType.CLAUDE_CODE),
    ("claude_code", ClientType.CLAUDE_CODE),
    ("claude code", ClientType.CLAUDE_CODE),
    ("codex", ClientType.CODEX),
    ("vscode", ClientType.VSCODE_CHAT),
    ("vs-code", ClientType.VSCODE_CHAT),
    ("deepseek-harness", ClientType.DSH),
    ("dsh", ClientType.DSH),
    ("opencode", ClientType.OPENCODE),
    ("muse", ClientType.BROWSER),
)


def detect_client_type(
    headers: Mapping[str, str] | None,
    launcher: str | None = None,
) -> ClientType:
    """Return the client type, or ``unknown`` when nothing proves it."""

    if launcher:
        mapped = _from_launcher(launcher)
        if mapped is not ClientType.UNKNOWN:
            return mapped
    if not headers:
        return ClientType.UNKNOWN
    for header in ("user-agent", "x-app", "x-client", "x-fcc-client"):
        value = _header(headers, header)
        if not value:
            continue
        for needle, client_type in _SIGNALS:
            if needle in value:
                return client_type
    return ClientType.UNKNOWN


def client_label(headers: Mapping[str, str] | None) -> str | None:
    """A short, non-identifying label for the client, when one is available."""

    if not headers:
        return None
    for header in ("x-fcc-client-label", "x-client-name"):
        value = _header(headers, header)
        if value:
            return value[:64]
    return None


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.casefold() == name:
            return str(value).strip().casefold()[:256]
    return ""


def _from_launcher(launcher: str) -> ClientType:
    needle = launcher.strip().casefold()
    for token, client_type in _SIGNALS:
        if token in needle:
            return client_type
    return ClientType.UNKNOWN
