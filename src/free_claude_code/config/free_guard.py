"""FreeGuard and Analytics configuration primitives.

The canonical enums live here, in the configuration layer, so that both
:mod:`free_claude_code.config.settings` and
:mod:`free_claude_code.application.free_guard` can refer to them without an import
cycle. The derived policy configuration lives in the application layer, where the
free-only rules are evaluated.
"""

from enum import StrEnum


class FreeGuardMode(StrEnum):
    """How strongly FreeGuard restricts the model universe.

    - ``off``: no new restrictions. Legacy behaviour is preserved exactly.
    - ``catalog_filter_only``: unconfirmed models are hidden from pickers and
      ``/v1/models``, but a direct model id is still attempted. The UI must warn
      loudly that direct calls are not protected.
    - ``strict_free_only``: deny-by-default before every upstream call.
    """

    OFF = "off"
    CATALOG_FILTER_ONLY = "catalog_filter_only"
    STRICT_FREE_ONLY = "strict_free_only"


FREE_GUARD_MODES: tuple[FreeGuardMode, ...] = (
    FreeGuardMode.OFF,
    FreeGuardMode.CATALOG_FILTER_ONLY,
    FreeGuardMode.STRICT_FREE_ONLY,
)


def parse_free_guard_mode(value: object) -> FreeGuardMode:
    """Coerce one submitted value into a mode, defaulting to ``off``."""

    if isinstance(value, FreeGuardMode):
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        for mode in FREE_GUARD_MODES:
            if mode.value == text:
                return mode
    return FreeGuardMode.OFF
