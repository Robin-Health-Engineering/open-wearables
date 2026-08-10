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

# Profile change (delete / unlink / update) — subscribed like any other appli,
# but handled inline by `_screen()` before domain routing, never fetched as data.
PROFILE_CHANGE_APPLI = 46

# Of appli 46's three `action` values, only these mean we lost access upstream
# and should revoke local connections; `update` is a metadata-only change.
PROFILE_CHANGE_REVOKING_ACTIONS = frozenset({"delete", "unlink"})

SUBSCRIBED_APPLIS: list[int] = sorted({*APPLI_DOMAIN, PROFILE_CHANGE_APPLI})
