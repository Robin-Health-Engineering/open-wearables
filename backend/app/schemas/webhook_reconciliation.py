"""Outcome of reconciling a provider's webhook subscriptions."""

from dataclasses import asdict, dataclass
from typing import Any

from app.services.providers.base_strategy import WebhookSubscriptionOwner


@dataclass(frozen=True)
class WebhookReconciliationResult:
    """Represent a reconciliation outcome across subscription ownership models."""

    provider: str
    owner: WebhookSubscriptionOwner | None = None
    dispatched: int = 0
    created: int = 0
    skipped: int = 0
    errors: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
