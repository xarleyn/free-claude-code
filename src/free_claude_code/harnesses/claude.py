"""Shared Claude Code environment policy for FCC client surfaces."""

from collections.abc import Mapping

from free_claude_code.harnesses.environment import client_environment

CLAUDE_CODE_AUTO_COMPACT_WINDOW = "190000"
CLAUDE_BINARY_NAME = "claude"
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"
_CLIENT_LABEL = "claude-code"


def claude_identity_headers(launch_id: str, existing: str | None = None) -> str:
    """`ANTHROPIC_CUSTOM_HEADERS` value: this launch's session plus the client name.

    Some upstreams bind a subscription or a free tier to a session rather than to the API
    key and reject a session-less call, so the proxy forwards whatever the caller carries
    and never invents one. A launched client is the caller, so the launch id is the value
    to carry. A header the user already set is preserved, not replaced.
    """

    ours = f"x-fcc-launch-id: {launch_id}\nx-fcc-client-label: {_CLIENT_LABEL}"
    prior = (existing or "").strip()
    return f"{prior}\n{ours}" if prior else ours


def claude_proxy_values(proxy_root_url: str, auth_token: str) -> dict[str, str]:
    """Settings shared by launched Claude sessions and the VS Code extension."""
    return {
        "ANTHROPIC_BASE_URL": proxy_root_url,
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        # FCC uses Claude's separate classifier requests, not Anthropic server checks.
        "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
        "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": CLAUDE_CODE_AUTO_COMPACT_WINDOW,
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_FEEDBACK_COMMAND": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }


def build_claude_proxy_env(
    *,
    proxy_root_url: str,
    auth_token: str,
    base_env: Mapping[str, str],
    launch_id: str | None = None,
) -> dict[str, str]:
    """Return the canonical environment for Claude Code proxy sessions."""

    values = claude_proxy_values(proxy_root_url, auth_token)
    if launch_id:
        # Read before `ANTHROPIC_` cleanup, which would drop the user's own value.
        prior = next(
            (
                value
                for key, value in base_env.items()
                if key.upper() == CUSTOM_HEADERS_ENV
            ),
            None,
        )
        values[CUSTOM_HEADERS_ENV] = claude_identity_headers(launch_id, prior)

    # Claude's aggregate traffic flag also suppresses gateway model discovery.
    return client_environment(
        base_env,
        proxy_root_url=proxy_root_url,
        remove_prefixes=("ANTHROPIC_",),
        remove_keys=("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",),
        updates=values,
    )
