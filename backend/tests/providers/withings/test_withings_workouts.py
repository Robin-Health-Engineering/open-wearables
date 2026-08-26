from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.constants.workout_types.withings import (
    DEFERRED_WITHINGS_CATEGORIES,
    OFFICIAL_WITHINGS_CATEGORY_IDS,
    WITHINGS_CATEGORY_MAP,
    get_unified_workout_type,
)
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.enums.workout_types import WorkoutType
from app.schemas.providers.withings import WithingsWorkout
from app.services.providers.withings._client import PaginatedResult
from app.services.providers.withings.workouts import WithingsWorkouts


def test_category_mapping() -> None:
    assert get_unified_workout_type(1) == WorkoutType.WALKING
    assert get_unified_workout_type(2) == WorkoutType.RUNNING
    assert get_unified_workout_type(6) == WorkoutType.CYCLING
    assert get_unified_workout_type(999999) == WorkoutType.OTHER
    # Authoritative category ids from the spec.
    assert get_unified_workout_type(12) == WorkoutType.TENNIS
    assert get_unified_workout_type(15) == WorkoutType.BADMINTON
    assert get_unified_workout_type(16) == WorkoutType.STRENGTH_TRAINING
    assert get_unified_workout_type(17) == WorkoutType.STRENGTH_TRAINING
    assert get_unified_workout_type(18) == WorkoutType.ELLIPTICAL
    # Withings' own "Other" is a declared category, not an unmapped id.
    assert get_unified_workout_type(36) == WorkoutType.GENERIC
    assert get_unified_workout_type(187) == WorkoutType.ROWING
    assert get_unified_workout_type(272) == WorkoutType.MULTISPORT
    assert get_unified_workout_type(308) == WorkoutType.INDOOR_CYCLING


def test_category_mapping_spec_extension() -> None:
    assert get_unified_workout_type(306) == WorkoutType.WALKING  # Indoor walk
    assert get_unified_workout_type(455) == WorkoutType.STAND_UP_PADDLEBOARDING
    assert get_unified_workout_type(456) == WorkoutType.PADEL
    assert get_unified_workout_type(494) == WorkoutType.KAYAKING
    assert get_unified_workout_type(496) == WorkoutType.SAILING
    assert get_unified_workout_type(498) == WorkoutType.TRAIL_RUNNING
    assert get_unified_workout_type(510) == WorkoutType.PICKLEBALL
    assert get_unified_workout_type(521) == WorkoutType.TRIATHLON
    assert get_unified_workout_type(523) == WorkoutType.MOUNTAIN_BIKING
    assert get_unified_workout_type(529) == WorkoutType.BACKCOUNTRY_SKIING
    assert get_unified_workout_type(547) == WorkoutType.INDOOR_CYCLING  # Spinclass
    assert get_unified_workout_type(548) == WorkoutType.CRICKET
    assert get_unified_workout_type(551) == WorkoutType.MEDITATION
    assert get_unified_workout_type(552) == WorkoutType.STRETCHING
    assert get_unified_workout_type(557) == WorkoutType.LACROSSE
    assert get_unified_workout_type(22) == WorkoutType.AMERICAN_FOOTBALL
    assert get_unified_workout_type(457) == WorkoutType.GAMING
    assert get_unified_workout_type(502) == WorkoutType.DIVING
    assert get_unified_workout_type(522) == WorkoutType.DIVING
    assert get_unified_workout_type(515) == WorkoutType.WHEELCHAIR
    assert get_unified_workout_type(516) == WorkoutType.WHEELCHAIR
    assert get_unified_workout_type(525) == WorkoutType.E_BIKING
    for category in (545, 553, 554, 568):
        assert get_unified_workout_type(category) == WorkoutType.CHORES


def test_walking_variants_map_to_walking() -> None:
    for category in (542, 543, 558, 559, 562, 563):
        assert get_unified_workout_type(category) == WorkoutType.WALKING


def test_category_mapping_generic_sport_fallbacks() -> None:
    for category in (500, 505, 506, 509, 511, 512, 514, 517, 520, 556, 565):
        assert get_unified_workout_type(category) == WorkoutType.SPORT


def test_category_mapping_adaptive_and_wellness() -> None:
    assert get_unified_workout_type(504) == WorkoutType.MULTISPORT  # Biathlon
    assert get_unified_workout_type(528) == WorkoutType.CYCLING  # Velomobile
    assert get_unified_workout_type(539) == WorkoutType.PARA_SPORTS  # Standing Frame
    assert get_unified_workout_type(540) == WorkoutType.STRENGTH_TRAINING  # Seated Strength
    assert get_unified_workout_type(541) == WorkoutType.CARDIO_TRAINING  # Seated Cardio
    assert get_unified_workout_type(555) == WorkoutType.LIFESTYLE  # Public Speaking
    assert get_unified_workout_type(560) == WorkoutType.MEDITATION  # Breathing exercises
    assert get_unified_workout_type(561) == WorkoutType.STRETCHING  # Balance Drills


def test_every_official_category_is_mapped_or_deferred() -> None:
    assert WITHINGS_CATEGORY_MAP.keys() <= OFFICIAL_WITHINGS_CATEGORY_IDS
    assert DEFERRED_WITHINGS_CATEGORIES.keys() <= OFFICIAL_WITHINGS_CATEGORY_IDS
    assert not WITHINGS_CATEGORY_MAP.keys() & DEFERRED_WITHINGS_CATEGORIES.keys()
    assert (WITHINGS_CATEGORY_MAP.keys() | DEFERRED_WITHINGS_CATEGORIES.keys()) == OFFICIAL_WITHINGS_CATEGORY_IDS
    assert all(reason for reason in DEFERRED_WITHINGS_CATEGORIES.values())
    for category in DEFERRED_WITHINGS_CATEGORIES:
        assert get_unified_workout_type(category) == WorkoutType.OTHER


def test_official_category_catalog_contains_all_130_ids() -> None:
    expected = {
        *range(1, 37),
        128,
        187,
        188,
        *range(191, 197),
        272,
        *range(306, 309),
        *range(455, 458),
        *range(490, 533),
        *range(534, 569),
    }
    assert len(OFFICIAL_WITHINGS_CATEGORY_IDS) == 130
    assert expected == OFFICIAL_WITHINGS_CATEGORY_IDS


def _make_workouts() -> WithingsWorkouts:
    return WithingsWorkouts(
        workout_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name="withings",
        api_base_url="https://wbsapi.withings.net",
        oauth=MagicMock(),
    )


def test_normalize_workout_builds_event_record() -> None:
    w = _make_workouts()
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    raw = WithingsWorkout.model_validate(
        {
            "id": 77,
            "category": 2,
            "startdate": start,
            "enddate": end,
            "timezone": "Asia/Kolkata",
            "data": {"steps": 5000, "calories": 300, "hr_average": 145},
        }
    )
    record, detail = w._normalize_workout(raw, uuid4())
    assert record.category == "workout"
    assert record.type == WorkoutType.RUNNING.value
    assert record.duration_seconds == 45 * 60
    assert record.external_id == "77"
    assert record.source == "withings"
    assert record.start_datetime == datetime(2024, 1, 15, 7, tzinfo=timezone.utc)
    assert record.end_datetime == datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc)
    assert record.zone_offset == "+05:30"
    assert detail.record_id == record.id


@patch("app.services.providers.withings.timezone.log_structured")
def test_normalize_workout_invalid_timezone_falls_back(mock_log: MagicMock) -> None:
    start = int(datetime(2024, 7, 15, 7, tzinfo=timezone.utc).timestamp())
    raw = WithingsWorkout.model_validate(
        {
            "id": 77,
            "category": 2,
            "startdate": start,
            "enddate": start + 60,
            "timezone": "Invalid/Zone",
        }
    )

    record, _ = _make_workouts()._normalize_workout(raw, uuid4())

    assert record.zone_offset is None
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["action"] == "workout_timezone_invalid"


@patch("app.services.providers.withings.timezone.log_structured")
def test_normalize_workout_missing_timezone_is_silent(mock_log: MagicMock) -> None:
    start = int(datetime(2024, 7, 15, 7, tzinfo=timezone.utc).timestamp())
    raw = WithingsWorkout.model_validate({"category": 2, "startdate": start, "enddate": start + 60})

    record, _ = _make_workouts()._normalize_workout(raw, uuid4())

    assert record.zone_offset is None
    mock_log.assert_not_called()


def test_normalize_workout_uses_end_offset_across_dst() -> None:
    start = int(datetime(2024, 3, 10, 6, 30, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 3, 10, 8, 0, tzinfo=timezone.utc).timestamp())
    raw = WithingsWorkout.model_validate(
        {
            "category": 2,
            "startdate": start,
            "enddate": end,
            "timezone": "America/New_York",
        }
    )

    record, _ = _make_workouts()._normalize_workout(raw, uuid4())

    assert record.zone_offset == "-04:00"


@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_saves_workouts(mock_api: MagicMock, mock_event: MagicMock) -> None:
    w = _make_workouts()
    db = MagicMock()
    connection_id = uuid4()
    w.connection_repo.get_active_connection.return_value = MagicMock(id=connection_id)
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    mock_api.return_value = [
        {"id": 77, "category": 2, "startdate": start, "enddate": end, "deviceid": "abc123", "data": {}}
    ]
    assert w.load_data(db, uuid4()) == 1
    mock_event.create.assert_called_once()
    mock_event.create_detail.assert_called_once()
    assert mock_event.create.call_args.args[1].user_connection_id == connection_id


@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_does_not_infer_source_from_missing_deviceid(mock_api: MagicMock, mock_event: MagicMock) -> None:
    w = _make_workouts()
    db = MagicMock()
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    missing_device = {"id": 1, "category": 2, "startdate": start, "enddate": end, "data": {}}
    mock_api.return_value = [missing_device]

    assert w.load_data(db, uuid4()) == 1
    mock_event.create.assert_called_once()
    mock_event.create_detail.assert_called_once()


def test_normalize_workout_rejects_inverted_window() -> None:
    w = _make_workouts()
    start = int(datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())  # 1h *before* start
    raw = WithingsWorkout.model_validate({"id": 5, "category": 2, "startdate": start, "enddate": end, "data": {}})
    with pytest.raises(ValueError, match="enddate must be after startdate"):
        w._normalize_workout(raw, uuid4())


@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_skips_inverted_window_workout(mock_api: MagicMock, mock_event: MagicMock) -> None:
    w = _make_workouts()
    db = MagicMock()
    start = int(datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    inverted = {"id": 9, "category": 2, "startdate": start, "enddate": end, "deviceid": "abc123", "data": {}}
    good = {
        "id": 10,
        "category": 2,
        "startdate": int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp()),
        "enddate": int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp()),
        "deviceid": "abc123",
        "data": {},
    }
    mock_api.return_value = [inverted, good]
    assert w.load_data(db, uuid4()) == 1
    mock_event.create.assert_called_once()


@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_rolls_back_on_save_failure(mock_api: MagicMock, mock_event: MagicMock) -> None:
    w = _make_workouts()
    db = MagicMock()
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    failing = {"id": 1, "category": 2, "startdate": start, "enddate": end, "deviceid": "abc123", "data": {}}
    good = {"id": 2, "category": 2, "startdate": start, "enddate": end, "deviceid": "abc123", "data": {}}
    mock_api.return_value = [failing, good]
    # First create() blows up mid-batch; the second succeeds.
    mock_event.create.side_effect = [Exception("db down"), MagicMock(id=uuid4())]

    result = w.load_data(db, uuid4())
    assert result == 1
    assert isinstance(result, WriteCounts)
    assert result.failed == 1
    assert result.skipped == 0
    db.rollback.assert_called_once()
    assert mock_event.create.call_count == 2


@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_skips_bad_workout(mock_api: MagicMock, mock_event: MagicMock) -> None:
    w = _make_workouts()
    db = MagicMock()
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    # First workout is malformed (missing "category" and "startdate") so _normalize_workout raises.
    bad_workout = {"id": 99, "enddate": end, "deviceid": "abc123", "data": {}}
    good_workout = {"id": 77, "category": 2, "startdate": start, "enddate": end, "deviceid": "abc123", "data": {}}
    mock_api.return_value = [bad_workout, good_workout]
    result = w.load_data(db, uuid4())
    assert result == 1
    assert isinstance(result, WriteCounts)
    assert result.skipped == 1
    assert result.failed == 0
    mock_event.create.assert_called_once()
    mock_event.create_detail.assert_called_once()


@patch("app.services.providers.withings.workouts.log_structured")
@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_skips_no_activity_without_unknown_warning(
    mock_api: MagicMock, mock_event: MagicMock, mock_log: MagicMock
) -> None:
    w = _make_workouts()
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    mock_api.return_value = [{"id": 77, "category": 128, "startdate": start, "enddate": end, "data": {}}]

    result = w.load_data(MagicMock(), uuid4())

    assert result == 0
    assert isinstance(result, WriteCounts)
    assert result.skipped == 1
    mock_event.create.assert_not_called()
    mock_log.assert_not_called()


@patch("app.services.providers.withings.workouts.log_structured")
@patch("app.services.providers.withings.workouts.event_record_service")
@patch.object(WithingsWorkouts, "get_workouts_from_api")
def test_load_data_warns_once_per_distinct_unknown_category(
    mock_api: MagicMock, mock_event: MagicMock, mock_log: MagicMock
) -> None:
    w = _make_workouts()
    start = int(datetime(2024, 1, 15, 7, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 15, 7, 45, tzinfo=timezone.utc).timestamp())
    categories = (36, 500, 999, 999, 1000)
    mock_api.return_value = [
        {"id": category, "category": category, "startdate": start, "enddate": end, "data": {}}
        for category in categories
    ]
    mock_event.create.side_effect = [MagicMock(id=uuid4()) for _ in categories]

    result = w.load_data(MagicMock(), uuid4())

    assert result == len(categories)
    # Catalogued ids keep their mapping; only the undocumented ones become OTHER.
    assert [call.args[1].type for call in mock_event.create.call_args_list] == [
        WorkoutType.GENERIC,  # 36 Other
        WorkoutType.SPORT,  # 500 Paintball
        WorkoutType.OTHER,
        WorkoutType.OTHER,
        WorkoutType.OTHER,
    ]
    unknown_warnings = [
        call for call in mock_log.call_args_list if call.kwargs.get("action") == "workout_category_unknown"
    ]
    assert [call.kwargs["category_id"] for call in unknown_warnings] == [999, 1000]


@patch("app.services.providers.withings.workouts.paginate")
def test_load_data_with_iso_dates_passes_ymd_to_paginate(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    w = _make_workouts()
    db = MagicMock()
    user_id = uuid4()

    w.load_data(db, user_id, start_date="2024-01-01T00:00:00+00:00", end_date="2024-01-08T00:00:00+00:00")

    assert mock_paginate.called, "paginate was not called"
    _, kwargs = mock_paginate.call_args
    passed_params = kwargs.get("params", {})
    assert kwargs["service_path"] == "/v2/measure"
    assert kwargs["action"] == "getworkouts"
    assert kwargs["list_key"] == "series"
    assert passed_params["data_fields"] == "calories,steps,distance,hr_average,hr_min,hr_max"
    # Both bounds carry the local-day widening.
    assert passed_params.get("startdateymd") == "2023-12-31", (
        f"Expected startdateymd='2023-12-31', got {passed_params.get('startdateymd')!r}"
    )
    assert passed_params.get("enddateymd") == "2024-01-09", (
        f"Expected enddateymd='2024-01-09', got {passed_params.get('enddateymd')!r}"
    )


@patch("app.services.providers.withings.workouts.paginate")
def test_get_workouts_widens_the_local_day_window(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    w = _make_workouts()
    w.get_workouts_from_api(
        MagicMock(),
        uuid4(),
        start_date=datetime(2018, 7, 2, tzinfo=timezone.utc).isoformat(),
        end_date=datetime(2018, 7, 2, 23, 0, tzinfo=timezone.utc).isoformat(),
    )
    params = mock_paginate.call_args.kwargs["params"]
    assert params["startdateymd"] == "2018-07-01"
    assert params["enddateymd"] == "2018-07-03"


def test_to_ymd_converts_iso_string() -> None:
    assert WithingsWorkouts._to_ymd("2024-01-01T00:00:00+00:00") == "2024-01-01"
    assert WithingsWorkouts._to_ymd("2024-03-15T12:30:00Z") == "2024-03-15"


def test_to_ymd_converts_datetime_object() -> None:
    dt = datetime(2024, 6, 20, 8, 0, tzinfo=timezone.utc)
    assert WithingsWorkouts._to_ymd(dt) == "2024-06-20"


def test_to_ymd_returns_none_for_falsy() -> None:
    assert WithingsWorkouts._to_ymd(None) is None
    assert WithingsWorkouts._to_ymd("") is None


def test_to_ymd_returns_none_for_unparseable() -> None:
    assert WithingsWorkouts._to_ymd("not-a-date") is None


@patch("app.services.providers.withings.workouts.paginate")
def test_get_workouts_sends_enddateymd_when_end_date_is_none(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    w = _make_workouts()

    w.get_workouts_from_api(MagicMock(), uuid4(), start_date="2024-01-01T00:00:00+00:00", end_date=None)

    params = mock_paginate.call_args.kwargs["params"]
    assert params["startdateymd"] == "2023-12-31"
    assert "enddateymd" in params, f"enddateymd was dropped from the request: {params!r}"
    assert params["enddateymd"] == (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


@patch("app.services.providers.withings.workouts.paginate")
def test_load_data_without_end_date_still_bounds_the_window(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    w = _make_workouts()

    w.load_data(MagicMock(), uuid4(), start_date="2024-01-01T00:00:00+00:00", end_date=None)

    params = mock_paginate.call_args.kwargs["params"]
    assert params["startdateymd"] == "2023-12-31"
    assert params["enddateymd"] == (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


@patch("app.services.providers.withings.workouts.paginate")
def test_get_workouts_defaults_to_thirty_day_window_when_no_dates(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    w = _make_workouts()

    w.get_workouts_from_api(MagicMock(), uuid4())

    params = mock_paginate.call_args.kwargs["params"]
    now = datetime.now(timezone.utc)
    assert params["enddateymd"] == (now + timedelta(days=1)).strftime("%Y-%m-%d")
    assert params["startdateymd"] == (now - timedelta(days=31)).strftime("%Y-%m-%d")


@patch("app.services.providers.withings.workouts.paginate")
def test_get_workouts_falls_back_to_bounded_window_for_unparseable_dates(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    w = _make_workouts()

    w.get_workouts_from_api(MagicMock(), uuid4(), start_date="not-a-date", end_date="also-not-a-date")

    params = mock_paginate.call_args.kwargs["params"]
    now = datetime.now(timezone.utc)
    assert params["startdateymd"] == (now - timedelta(days=31)).strftime("%Y-%m-%d")
    assert params["enddateymd"] == (now + timedelta(days=1)).strftime("%Y-%m-%d")


@patch("app.services.providers.withings.workouts.log_structured")
def test_to_ymd_logs_warning_for_unparseable_but_not_for_missing(mock_log: MagicMock) -> None:
    assert WithingsWorkouts._to_ymd("not-a-date") is None
    assert mock_log.call_count == 1
    assert mock_log.call_args.args[1] == "warning"
    assert mock_log.call_args.kwargs["action"] == "workout_date_parse_failed"

    mock_log.reset_mock()
    assert WithingsWorkouts._to_ymd(None) is None
    assert WithingsWorkouts._to_ymd("") is None
    mock_log.assert_not_called()
