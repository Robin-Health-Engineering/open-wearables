"""Route Withings notification applis to fetch domains.

``appli`` notification categories and ``meastype`` measure codes are distinct
numeric namespaces.
"""

from typing import Literal

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
