"""
Bounded recovery budget used by browser actions.

The executor uses max_retries as the canonical retry configuration.
The retries property remains available for compatibility with older code.
"""

from __future__ import annotations

from config import settings


class RetryBudget:
    """Track retries used during one task execution."""

    def __init__(
        self,
        max_retries: int | None = None,
    ) -> None:

        value = (
            settings.BROWSER_MAX_RETRIES
            if max_retries is None
            else max_retries
        )

        try:
            value = int(value)

        except (
            TypeError,
            ValueError,
        ):
            value = 1

        self.max_retries = max(
            0,
            value,
        )

        self.used = 0

    @property
    def retries(self) -> int:
        """
        Compatibility alias for code that still accesses `retries`.
        """

        return self.max_retries

    @property
    def remaining(self) -> int:
        return max(
            0,
            self.max_retries - self.used,
        )

    def can_retry(self) -> bool:
        return (
            self.used
            < self.max_retries
        )

    def consume(self) -> bool:

        if not self.can_retry():
            return False

        self.used += 1

        return True