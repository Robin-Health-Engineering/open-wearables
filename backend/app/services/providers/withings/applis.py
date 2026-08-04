"""Route Withings notification applis to fetch domains.

``appli`` notification categories and ``meastype`` measure codes are distinct
numeric namespaces.
"""

from typing import Literal
from urllib.parse import urlencode

from app.config import settings

Domain = Literal["measures", "sleep", "activity_workouts"]

APPLI_DOMAIN: dict[int, Domain] = {
    1: "measures",  # Body and Weight
    2: "measures",  # Temperature
    4: "measures",  # Blood Pressure and Heart Rate
    16: "activity_workouts",  # Activity and workouts
    44: "sleep",
    58: "measures",  # Glucose
}

# Profile change (delete / unlink / update) — handled inline, never subscribed.
PROFILE_CHANGE_APPLI = 46

# Per-user subscription set: routing keys == subscriptions, by construction.
SUBSCRIBED_APPLIS: list[int] = sorted(APPLI_DOMAIN)


def withings_callback_url() -> str:
    """Authenticated callback URL Withings POSTs to and HEAD-probes."""
    # Withings recommends an unguessable token on the exact callback URL:
    # https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-overview/#verify-a-shared-secret
    token = settings.withings_webhook_token
    if token is None or not token.get_secret_value():
        raise ValueError("WITHINGS_WEBHOOK_TOKEN must be configured for Withings notifications")
    query = urlencode({"token": token.get_secret_value()})
    return f"{settings.api_base_url}{settings.api_v1}/providers/withings/webhooks?{query}"
