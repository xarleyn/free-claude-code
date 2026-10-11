"""Session and client identity carried by a request, and what upstreams receive.

Several upstreams bind usage, free tiers and rate limits to a session rather than to the
API key alone, and they reject a request that names no session (OpenCode answers
``MissingSessionID``). Callers and FCC's own launchers already carry an id and a client
name, so those values are forwarded as they arrived.

Nothing here invents identity: a request that names no session reaches upstream with no
session. Binding one account's usage to a session its caller does not own is worse than
the rejection.

One module owns the header spellings, so a provider maps to whatever name its upstream
expects instead of each keeping a private list.
"""

from collections.abc import Mapping
from dataclasses import dataclass

#: Inbound session-id headers, in precedence order. The provider's own spelling wins over
#: generic ones, then the client integration names, then the launcher id as the last
#: stand-in — this is the order the existing OpenCode contract tests pin down.
SESSION_HEADERS: tuple[str, ...] = (
    "x-opencode-session",
    "session-id",
    "x-session-id",
    "x-claude-code-session-id",
    "session_id",
    "x-grok-session-id",
    "x-meta-ai-gateway-session-id",
    "x-tbh-session-id",
    "x-fcc-launch-id",
    "anthropic-session-id",
    "x-anthropic-session-id",
    "claude-session-id",
    "x-claude-session-id",
)

#: Inbound client-name headers, in precedence order. These are the names FCC's launcher
#: configs and integrations write. ``user-agent`` is forwarded untouched by each provider
#: and is deliberately not a name to invent from.
CLIENT_NAME_HEADERS: tuple[str, ...] = (
    "x-fcc-client-label",
    "x-client-name",
    "x-fcc-client",
    "x-client",
    "x-app",
)

#: Canonical outbound names for a caller that named itself.
SESSION_HEADER_OUT = "x-session-id"
CLIENT_NAME_HEADER_OUT = "x-client-name"

#: Per-provider outbound session spellings, on top of the canonical one.
PROVIDER_SESSION_HEADERS: Mapping[str, tuple[str, ...]] = {
    "opencode_zen": ("x-opencode-session",),
    "opencode_go": ("x-opencode-session",),
}

_MAX_VALUE_LENGTH = 256


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not text.isascii():
        return None
    return text[:_MAX_VALUE_LENGTH]


def _first_header(
    headers: Mapping[str, str] | None, names: tuple[str, ...]
) -> str | None:
    if not headers:
        return None
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = _clean(lowered.get(name))
        if value is not None:
            return value
    return None


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """One session id and client name to show upstream, exactly as the caller gave them."""

    session_id: str | None
    client_name: str | None

    def headers_for(self, provider_id: str | None = None) -> dict[str, str]:
        """Outbound headers for one provider; absent values send nothing.

        A provider with a declared spelling gets exactly that one header, so an upstream
        that names its session header is not also handed a generic copy of it. Everyone
        else gets the canonical name.
        """

        aliases = PROVIDER_SESSION_HEADERS.get(provider_id or ())
        headers: dict[str, str] = {}
        if self.session_id:
            for name in aliases or (SESSION_HEADER_OUT,):
                headers[name] = self.session_id
        if self.client_name:
            headers[CLIENT_NAME_HEADER_OUT] = self.client_name
        return headers


def resolve_identity(request_headers: Mapping[str, str] | None) -> RequestIdentity:
    """Read the identity one request carries, without supplying anything of our own."""

    return RequestIdentity(
        session_id=_first_header(request_headers, SESSION_HEADERS),
        client_name=_first_header(request_headers, CLIENT_NAME_HEADERS),
    )
