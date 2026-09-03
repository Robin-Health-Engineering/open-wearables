"""Model the Withings payload fields required by ingestion."""

from pydantic import BaseModel, Field


class WithingsMeasure(BaseModel):
    """``measure_object``: the real value is ``value × 10^unit``."""

    value: int
    type: int
    unit: int
    position: int | None = None


class WithingsMeasureGroup(BaseModel):
    """``measuregrp_object`` — one timestamped group of measures."""

    date: int
    # Payloads may place the timezone on the group or only on the response body.
    timezone: str | None = None
    measures: list[WithingsMeasure] = []
    grpid: int | None = None
    # attrib 0/8 = device-captured & unambiguous, 2/4 = manual entry (see spec table).
    attrib: int | None = None
    category: int | None = None
    deviceid: str | None = None
    model: str | None = None


class WithingsActivity(BaseModel):
    """``activity_object`` — a daily aggregate from ``getactivity``."""

    # Date of the aggregated data, ``YYYY-MM-DD``.
    date: str
    timezone: str | None = None
    # deviceid identifies the capturing device but may be absent on valid rows;
    # the echo filter is brand == 18, not deviceid absence.
    deviceid: str | None = None
    # Origin signals: brand 1 = Withings, 18 = external/echo (e.g. Health Connect).
    # is_tracker = captured by Withings hardware.
    brand: int | None = None
    is_tracker: bool | None = None
    steps: int | None = None
    distance: float | None = None
    calories: float | None = None  # active kcal
    totalcalories: float | None = None  # active + passive kcal


class WithingsSleepData(BaseModel):
    """``sleep_summary_object.data`` — the fields we request via ``data_fields``.

    Durations are nullable: the spec nulls light/deep/REM for nights that come
    from an external source.
    """

    total_timeinbed: int | None = None
    total_sleep_time: int | None = None
    asleepduration: int | None = None
    deepsleepduration: int | None = None
    lightsleepduration: int | None = None
    remsleepduration: int | None = None
    wakeupduration: int | None = None
    # Ratio of total sleep time over time in bed, 0.0–1.0 per spec.
    sleep_efficiency: float | None = None


class WithingsSleepSummary(BaseModel):
    """``sleep_summary_object`` — one night/session from ``getsummary``."""

    startdate: int
    enddate: int
    id: int | None = None
    date: str | None = None
    timezone: str | None = None
    # model 16 = tracker, 32 = Sleep Monitor (sleep summaries carry no deviceid).
    model: int | None = None
    model_id: int | None = None
    data: WithingsSleepData = Field(default_factory=WithingsSleepData)


class WithingsWorkoutData(BaseModel):
    """``workout_object.data`` — the fields we request via ``data_fields``."""

    calories: float | None = None
    steps: int | None = None
    distance: float | None = None
    elevation: float | None = None
    hr_average: int | None = None
    hr_min: int | None = None
    hr_max: int | None = None


class WithingsWorkout(BaseModel):
    """``workout_object`` — one session from ``getworkouts``."""

    category: int
    startdate: int
    enddate: int
    id: int | None = None
    attrib: int | None = None
    date: str | None = None
    timezone: str | None = None
    # Workouts retain rows without deviceid and do not apply the activity echo filter.
    deviceid: str | None = None
    data: WithingsWorkoutData = Field(default_factory=WithingsWorkoutData)
