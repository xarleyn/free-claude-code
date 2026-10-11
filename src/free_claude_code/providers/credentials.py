"""Multiple credentials per provider, and rotation when one of them runs out.

Operators often hold several accounts at the same provider: one quota spent is not a
reason to fail the request or to fall through to a different model. A pool keeps the keys
in configuration order, uses one while it is healthy, and cool downs the key that just
failed for as long as the upstream asked (or a floor of its own), then continues on the
next key.

The pool never logs or returns key material: callers see a fingerprint.
"""

import asyncio
import hashlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

#: How long a key is set aside when the upstream gives no `Retry-After`. Long enough that a
#: second request in the same conversation does not immediately re-bill a spent account.
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 15 * 60.0

_KEY_SEPARATORS = (",", ";", "\n", "\r")


def parse_credentials(value: str | None) -> tuple[str, ...]:
    """Split one configured value into keys; a single key keeps a single-key pool.

    Configuration keeps one field per provider, so several accounts are written in the
    same value, separated by commas or newlines. Empty and repeated entries are dropped:
    a duplicated key would look like a second account and cool down twice as often.
    """

    if not value:
        return ()
    parts = [value]
    for separator in _KEY_SEPARATORS:
        parts = [part for chunk in parts for part in chunk.split(separator)]
    keys: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def credential_fingerprint(key: str) -> str:
    """A stable, non-reversible identifier for one credential."""

    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class _KeyState:
    key: str
    cooldown_until: float = 0.0
    consecutive_failures: int = 0


@dataclass(slots=True)
class CredentialPool:
    """Ordered credentials for one provider, with per-key cooldown bookkeeping.

    A pool with one key behaves exactly like no pool at all: nothing is ever rotated, and
    `rotate_to_next` always refuses. That keeps single-key setups on today's code path.
    """

    provider_id: str
    states: list[_KeyState] = field(default_factory=list)
    _active: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.states and self._active >= len(self.states):
            self._active = 0

    @classmethod
    def for_provider(cls, provider_id: str, keys: Iterable[str]) -> CredentialPool:
        return cls(
            provider_id=provider_id,
            states=[_KeyState(key=key) for key in keys if key.strip()],
        )

    def sync_keys(self, keys: Iterable[str]) -> None:
        """Adopt a new configuration without losing the cooldowns that still apply.

        A settings reload can add, drop or reorder accounts. Cooldown state belongs to the
        key, not to its position, so a key that survives keeps its penalty and a removed key
        stops being tried at all. An empty set leaves the pool inert.
        """

        incoming = [key for key in keys if key.strip()]
        if [state.key for state in self.states] == incoming:
            return
        previous = {state.key: state for state in self.states}
        self.states = [
            _KeyState(
                key=key,
                cooldown_until=(
                    previous[key].cooldown_until if key in previous else 0.0
                ),
                consecutive_failures=(
                    previous[key].consecutive_failures if key in previous else 0
                ),
            )
            for key in incoming
        ]
        self._active = min(self._active, max(0, len(self.states) - 1))

    @property
    def size(self) -> int:
        return len(self.states)

    @property
    def rotates(self) -> bool:
        return len(self.states) > 1

    @property
    def current(self) -> str | None:
        """The credential in use now, or ``None`` when nothing is configured."""

        if not self.states:
            return None
        return self.states[min(self._active, len(self.states) - 1)].key

    async def credential(self) -> str:
        """Awaitable form for SDK credential providers, which call it per request."""

        return self.current or ""

    def healthy_count(self, *, now: float | None = None) -> int:
        moment = self._moment(now)
        return sum(1 for state in self.states if state.cooldown_until <= moment)

    async def rotate_to_next(
        self,
        *,
        credential_exhausted: bool,
        retry_after_seconds: float | None = None,
        now: float | None = None,
    ) -> str | None:
        """Set the failing credential aside and return the next key to try.

        `credential_exhausted` is the caller's classification: only a failure about the
        credential rotates, because a model or request failure would be answered the same way
        by every account. Returns ``None`` when nothing can be rotated: a single-key provider,
        a failure that is not about the credential, or every remaining key already cooling down.
        """

        if not self.rotates or not credential_exhausted:
            return None
        async with self._lock:
            moment = self._moment(now)
            active = self.states[min(self._active, len(self.states) - 1)]
            active.cooldown_until = moment + self._cooldown(retry_after_seconds)
            active.consecutive_failures += 1
            for offset in range(1, len(self.states)):
                candidate = self.states[(self._active + offset) % len(self.states)]
                if candidate.cooldown_until <= moment:
                    self._active = (self._active + offset) % len(self.states)
                    return candidate.key
            # Everything is cooling: stay on the shortest-freeze key so the next request
            # retries the one that recovers first, instead of pinning to a spent account.
            earliest = min(
                range(len(self.states)),
                key=lambda index: self.states[index].cooldown_until,
            )
            self._active = earliest
            return None

    def succeeded(self, *, now: float | None = None) -> None:
        """Clear cooldowns after a credential served the call."""

        for state in self.states:
            state.cooldown_until = 0.0
            state.consecutive_failures = 0

    def describe(self, *, now: float | None = None) -> list[dict[str, object]]:
        """Admin-visible state: fingerprints and cooldowns, never key material."""

        moment = self._moment(now)
        return [
            {
                "fingerprint": credential_fingerprint(state.key),
                "active": index == self._active,
                "cooling_down_seconds": max(0.0, state.cooldown_until - moment),
                "consecutive_failures": state.consecutive_failures,
            }
            for index, state in enumerate(self.states)
        ]

    @staticmethod
    def _moment(now: float | None) -> float:
        return time.monotonic() if now is None else now

    @staticmethod
    def _cooldown(retry_after_seconds: float | None) -> float:
        if retry_after_seconds is None:
            return DEFAULT_COOLDOWN_SECONDS
        return max(0.0, min(float(retry_after_seconds), MAX_COOLDOWN_SECONDS))
