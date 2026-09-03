"""Withings data API request definitions shared by ingestion services."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WithingsDataRequest:
    service_path: str
    action: str
    list_key: str
    data_fields: tuple[str, ...] = ()


MEASURES = WithingsDataRequest(
    service_path="/measure",
    action="getmeas",
    list_key="measuregrps",
)

ACTIVITY = WithingsDataRequest(
    service_path="/v2/measure",
    action="getactivity",
    list_key="activities",
    # Request persisted fields only; totalcalories derives passive calories.
    data_fields=(
        "steps",
        "distance",
        "calories",
        "totalcalories",
    ),
)

SLEEP_SUMMARY = WithingsDataRequest(
    service_path="/v2/sleep",
    action="getsummary",
    list_key="series",
    data_fields=(
        "total_timeinbed",
        "total_sleep_time",
        "asleepduration",
        "deepsleepduration",
        "lightsleepduration",
        "remsleepduration",
        "wakeupduration",
        "sleep_efficiency",
    ),
)

WORKOUTS = WithingsDataRequest(
    service_path="/v2/measure",
    action="getworkouts",
    list_key="series",
    data_fields=(
        "calories",
        "steps",
        "distance",
        "hr_average",
        "hr_min",
        "hr_max",
    ),
)
