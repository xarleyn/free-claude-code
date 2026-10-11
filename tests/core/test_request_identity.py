"""Identity pass-through: the caller's values win, and nothing is invented."""

from free_claude_code.core.request_identity import (
    resolve_identity,
)


def test_provider_spelling_beats_generic_and_launcher_names() -> None:
    identity = resolve_identity(
        {
            "X-Fcc-Launch-Id": "launcher-fallback",
            "session-id": "native-session",
            "x-opencode-session": "explicit-session",
        }
    )

    assert identity.session_id == "explicit-session"
    assert identity.headers_for("opencode_go") == {
        "x-opencode-session": "explicit-session"
    }


def test_launcher_id_is_the_last_session_name_to_be_used() -> None:
    identity = resolve_identity(
        {"x-fcc-launch-id": "launch-a", "session-id": "", "session_id": "sdk-session"}
    )

    assert identity.session_id == "sdk-session"


def test_sessionless_request_is_forwarded_without_any_session() -> None:
    identity = resolve_identity({"x-private": "not-a-session", "session-id": "   "})

    assert identity.session_id is None
    assert identity.client_name is None
    assert identity.headers_for("opencode_zen") == {}


def test_a_catalog_provider_without_a_declared_spelling_is_shown_no_identity() -> None:
    identity = resolve_identity(
        {"session-id": "s-1", "x-fcc-client-label": "claude-code/desktop"}
    )

    assert identity.session_id == "s-1"
    assert identity.client_name == "claude-code/desktop"
    assert identity.headers_for("open_router") == {}
    assert identity.headers_for(None) == {}


def test_a_custom_provider_gets_the_canonical_names_the_caller_supplied() -> None:
    identity = resolve_identity(
        {"session-id": "s-1", "x-fcc-client-label": "claude-code/desktop"}
    )

    assert identity.headers_for("custom_" + "0" * 32) == {
        "x-session-id": "s-1",
        "x-client-name": "claude-code/desktop",
    }


def test_non_ascii_and_oversized_values_are_not_forwarded() -> None:
    identity = resolve_identity(
        {
            "session-id": "сессия",
            "x-client-name": "x" * 400,
        }
    )

    assert identity.session_id is None
    assert identity.client_name == "x" * 256
