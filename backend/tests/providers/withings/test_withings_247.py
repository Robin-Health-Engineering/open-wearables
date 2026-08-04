from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import settings
from app.schemas.enums import SeriesType
from app.services.providers.withings._client import PaginatedResult
from app.services.providers.withings.coverage import MEASURE_TYPE_MAP
from app.services.providers.withings.data_247 import Withings247Data, WithingsDataSyncError


def _make_data_247() -> Withings247Data:
    return Withings247Data(provider_name="withings", api_base_url="https://wbsapi.withings.net", oauth=MagicMock())


def test_normalize_measures_maps_types_and_scales() -> None:
    d = _make_data_247()
    user_id = uuid4()
    groups = [
        {
            "date": 1728000000,
            "measures": [
                {"value": 7500, "type": 1, "unit": -2},  # weight 75.00 kg
                {"value": 120, "type": 10, "unit": 0},  # systolic 120
                {"value": 80, "type": 9, "unit": 0},  # diastolic 80
                {"value": 98, "type": 119, "unit": 0},  # glucose 98 mg/dL
                {"value": 45, "type": 155, "unit": 0},  # vascular age 45 years
            ],
        },
    ]
    samples = d.normalize_measures(groups, user_id)
    by_type = {s.series_type: s for s in samples}
    assert by_type[SeriesType.weight].value == Decimal("75.00")
    assert by_type[SeriesType.weight].source == "withings"
    assert by_type[SeriesType.blood_pressure_systolic].value == Decimal("120")
    assert by_type[SeriesType.blood_pressure_diastolic].value == Decimal("80")
    assert by_type[SeriesType.blood_glucose].value == Decimal("98")
    assert by_type[SeriesType.cardiovascular_age].value == Decimal("45")
    expected_ts = datetime.fromtimestamp(1728000000, tz=timezone.utc)
    assert by_type[SeriesType.weight].recorded_at == expected_ts


def test_normalize_measures_keeps_mapped_and_drops_deferred_official_types() -> None:
    d = _make_data_247()
    groups = [
        {
            "date": 1728000000,
            "measures": [
                {"value": 1500, "type": 88, "unit": -2},  # bone mass
                {"value": 1000, "type": 91, "unit": -2},  # pulse wave velocity
                {"value": 42000, "type": 77, "unit": -3},  # total body water
                {"value": 1, "type": 130, "unit": 0},  # AFib classification
                {"value": 12, "type": 196, "unit": 0},  # Nerve Response Score
                {"value": 1800, "type": 226, "unit": 0},  # BMR rate
                {"value": 40, "type": 227, "unit": 0},  # metabolic age
                {"value": 7500, "type": 1, "unit": -2},  # weight 75.00 kg → kept
            ],
        },
    ]
    samples = d.normalize_measures(groups, uuid4())
    assert {s.series_type for s in samples} == {
        SeriesType.weight,
        SeriesType.bone_mass,
        SeriesType.withings_pulse_wave_velocity,
        SeriesType.body_water_mass,
        SeriesType.withings_metabolic_age,
    }


def test_normalize_measures_converts_height_metres_to_cm() -> None:
    # Withings height (meastype 4) is metres; OW `height` series is centimetres.
    d = _make_data_247()
    groups = [{"date": 1728000000, "measures": [{"value": 180, "type": 4, "unit": -2}]}]  # 1.80 m
    by_type = {s.series_type: s for s in d.normalize_measures(groups, uuid4())}
    assert by_type[SeriesType.height].value == Decimal("180.00")


@pytest.mark.parametrize(
    "measures",
    [
        [
            {"value": 215, "type": 12, "unit": -1},
            {"value": 370, "type": 71, "unit": -1},
        ],
        [
            {"value": 370, "type": 71, "unit": -1},
            {"value": 215, "type": 12, "unit": -1},
        ],
    ],
)
def test_normalize_measures_keeps_body_temperature_independent_of_generic_temperature_order(
    measures: list[dict[str, int]],
) -> None:
    samples = _make_data_247().normalize_measures([{"date": 1728000000, "measures": measures}], uuid4())

    assert [(sample.series_type, sample.value) for sample in samples] == [
        (SeriesType.body_temperature, Decimal("37.0"))
    ]


@patch("app.services.providers.withings.data_247.timeseries_service")
@patch("app.services.providers.withings.data_247.paginate")
def test_save_measures_persists_samples(mock_paginate: MagicMock, mock_ts: MagicMock) -> None:
    d = _make_data_247()
    db = MagicMock()
    connection_id = uuid4()
    mock_paginate.return_value = PaginatedResult(
        rows=[{"date": 1728000000, "measures": [{"value": 7500, "type": 1, "unit": -2}]}], envelope={}
    )
    with patch.object(d.connection_repo, "get_active_connection", return_value=MagicMock(id=connection_id)):
        count = d.save_measures(db, uuid4(), datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert count == 1
    assert mock_paginate.call_args.kwargs["service_path"] == "/measure"
    assert mock_paginate.call_args.kwargs["action"] == "getmeas"
    assert mock_paginate.call_args.kwargs["list_key"] == "measuregrps"
    requested = {int(code) for code in mock_paginate.call_args.kwargs["params"]["meastypes"].split(",")}
    assert requested == set(MEASURE_TYPE_MAP)
    mock_ts.bulk_create_samples.assert_called_once()
    assert mock_ts.bulk_create_samples.call_args.args[1][0].user_connection_id == connection_id


def test_normalize_activity_maps_metrics() -> None:
    d = _make_data_247()
    rows = [
        {
            "date": "2024-01-15",
            "steps": 8000,
            "distance": 6000.0,
            "calories": 350.0,
            "totalcalories": 2200.0,
            "deviceid": "abc123",
        }
    ]
    samples = d.normalize_activity(rows, uuid4())
    by_type = {s.series_type: s for s in samples}
    assert by_type[SeriesType.steps].value == Decimal("8000")
    assert by_type[SeriesType.distance_walking_running].value == Decimal("6000.0")
    assert by_type[SeriesType.energy].value == Decimal("350.0")
    assert by_type[SeriesType.basal_energy].value == Decimal("1850.0")
    assert by_type[SeriesType.steps].recorded_at == datetime(2024, 1, 15, tzinfo=timezone.utc)
    assert by_type[SeriesType.steps].is_daily_total is True
    assert by_type[SeriesType.distance_walking_running].is_daily_total is True
    assert by_type[SeriesType.energy].is_daily_total is True
    assert by_type[SeriesType.basal_energy].is_daily_total is True


def test_normalize_activity_anchors_daily_totals_to_the_local_day_start() -> None:
    samples = _make_data_247().normalize_activity(
        [{"date": "2024-01-15", "timezone": "Pacific/Auckland", "steps": 8000}],
        uuid4(),
    )

    sample = samples[0]
    assert sample.recorded_at == datetime(2024, 1, 14, 11, 0, tzinfo=timezone.utc)
    assert sample.zone_offset == "+13:00"
    # recorded_at + zone_offset is how the repository derives the local day.
    assert sample.recorded_at + timedelta(hours=13) == datetime(2024, 1, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize("row_timezone", [None, "Not/AZone"])
def test_normalize_activity_falls_back_to_utc_midnight_without_a_usable_zone(row_timezone: str | None) -> None:
    row: dict = {"date": "2024-01-15", "steps": 8000}
    if row_timezone is not None:
        row["timezone"] = row_timezone

    sample = _make_data_247().normalize_activity([row], uuid4())[0]

    assert sample.recorded_at == datetime(2024, 1, 15, tzinfo=timezone.utc)
    assert sample.zone_offset is None


def test_normalize_measures_carry_the_groups_zone_offset() -> None:
    groups = [
        {"date": 1720000000, "timezone": "Pacific/Auckland", "measures": [{"value": 7500, "type": 1, "unit": -2}]},
        {"date": 1720000000, "measures": [{"value": 7600, "type": 1, "unit": -2}]},
    ]

    zoned, unzoned = _make_data_247().normalize_measures(groups, uuid4())

    assert zoned.recorded_at == datetime.fromtimestamp(1720000000, tz=timezone.utc)
    assert zoned.zone_offset == "+12:00"
    assert unzoned.zone_offset is None


def test_normalize_measures_falls_back_to_the_response_zone() -> None:
    groups = [{"date": 1720000000, "measures": [{"value": 7500, "type": 1, "unit": -2}]}]

    (sample,) = _make_data_247().normalize_measures(groups, uuid4(), default_timezone="Pacific/Auckland")

    assert sample.zone_offset == "+12:00"


def test_normalize_measures_prefers_the_groups_own_zone_over_the_response_zone() -> None:
    groups = [
        {"date": 1720000000, "timezone": "Pacific/Auckland", "measures": [{"value": 7500, "type": 1, "unit": -2}]}
    ]

    (sample,) = _make_data_247().normalize_measures(groups, uuid4(), default_timezone="Europe/Berlin")

    assert sample.zone_offset == "+12:00"


@patch("app.services.providers.withings.data_247.timeseries_service")
@patch("app.services.providers.withings.data_247.paginate")
def test_save_measures_stamps_unzoned_groups_with_the_response_zone(
    mock_paginate: MagicMock, mock_ts: MagicMock
) -> None:
    d = _make_data_247()
    mock_paginate.return_value = MagicMock(
        rows=[{"date": 1720000000, "measures": [{"value": 7500, "type": 1, "unit": -2}]}],
        envelope={"timezone": "Pacific/Auckland"},
    )
    # The write count is irrelevant here; this asserts what reaches normalisation.
    mock_ts.bulk_create_samples.return_value = 1

    with patch.object(d.connection_repo, "get_active_connection", return_value=MagicMock(id=uuid4())):
        d.save_measures(MagicMock(), uuid4(), datetime.now(timezone.utc), datetime.now(timezone.utc))

    (sample,) = mock_ts.bulk_create_samples.call_args.args[1]
    assert sample.zone_offset == "+12:00"


def test_normalize_activity_drops_invalid_negative_passive_calories() -> None:
    samples = _make_data_247().normalize_activity(
        [{"date": "2024-01-15", "calories": 500.0, "totalcalories": 400.0}],
        uuid4(),
    )

    assert {sample.series_type for sample in samples} == {SeriesType.energy}


def test_normalize_activity_skips_externally_sourced_rows() -> None:
    d = _make_data_247()
    rows = [
        {"date": "2024-01-15", "steps": 8000, "brand": 18, "deviceid": "xyz"},  # external source
        {"date": "2024-01-16", "steps": 9000, "brand": None, "deviceid": "abc123"},  # valid Withings
        {"date": "2024-01-17", "steps": 7000},  # brand absent → valid Withings
        {"date": "2024-01-18", "steps": 6000, "brand": 1},  # explicit Withings brand
    ]
    samples = d.normalize_activity(rows, uuid4())
    days = {s.recorded_at.date() for s in samples}
    assert days == {
        datetime(2024, 1, 16).date(),
        datetime(2024, 1, 17).date(),
        datetime(2024, 1, 18).date(),
    }
    assert all(s.series_type == SeriesType.steps for s in samples)


@patch("app.services.providers.withings.data_247.event_record_service")
@patch("app.services.providers.withings.data_247.paginate")
def test_save_sleep_creates_event_record(mock_paginate: MagicMock, mock_event: MagicMock) -> None:
    d = _make_data_247()
    db = MagicMock()
    # start 22:00, end 06:00 next day → 8h in bed
    start = int(datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 16, 6, 0, tzinfo=timezone.utc).timestamp())
    mock_paginate.return_value = PaginatedResult(
        rows=[
            {
                "id": 42,
                "startdate": start,
                "enddate": end,
                "timezone": "Europe/Berlin",
                "data": {
                    "deepsleepduration": 7200,
                    "lightsleepduration": 14400,
                    "remsleepduration": 5400,
                    "wakeupduration": 1800,
                    "sleep_efficiency": 0.9,
                },
            }
        ],
        envelope={},
    )
    count = d.save_sleep(
        db, uuid4(), datetime(2024, 1, 15, tzinfo=timezone.utc), datetime(2024, 1, 16, tzinfo=timezone.utc)
    )
    assert count == 1
    mock_event.create_or_merge_sleep.assert_called_once()
    call = mock_event.create_or_merge_sleep.call_args
    record = call.args[2]
    detail = call.args[3]
    threshold = call.args[4]
    assert record.category == "sleep"
    assert record.source == "withings"
    assert record.start_datetime == datetime(2024, 1, 15, 22, tzinfo=timezone.utc)
    assert record.end_datetime == datetime(2024, 1, 16, 6, tzinfo=timezone.utc)
    assert record.zone_offset == "+01:00"
    # 0–1 ratio stored on the 0–100 scale
    assert detail.sleep_efficiency_score == Decimal("90.0")
    assert threshold == settings.sleep_end_gap_minutes


@patch("app.services.providers.withings.data_247.event_record_service")
def test_sleep_uses_end_offset_across_dst(mock_event: MagicMock) -> None:
    start = int(datetime(2024, 3, 10, 6, 30, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 3, 10, 8, 0, tzinfo=timezone.utc).timestamp())

    assert _make_data_247()._save_sleep_row(
        MagicMock(),
        uuid4(),
        {
            "id": 42,
            "startdate": start,
            "enddate": end,
            "timezone": "America/New_York",
            "data": {"total_sleep_time": 5400, "total_timeinbed": 5400},
        },
        None,
    )

    record = mock_event.create_or_merge_sleep.call_args.args[2]
    assert record.zone_offset == "-04:00"


@patch("app.services.providers.withings.timezone.log_structured")
@patch("app.services.providers.withings.data_247.event_record_service")
def test_sleep_timezone_fallbacks(mock_event: MagicMock, mock_log: MagicMock) -> None:
    start = int(datetime(2024, 1, 15, 22, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 16, 6, tzinfo=timezone.utc).timestamp())
    data = {"total_sleep_time": 28800, "total_timeinbed": 28800}
    handler = _make_data_247()

    assert handler._save_sleep_row(MagicMock(), uuid4(), {"startdate": start, "enddate": end, "data": data}, None)
    assert handler._save_sleep_row(
        MagicMock(),
        uuid4(),
        {"startdate": start, "enddate": end, "timezone": "Invalid/Zone", "data": data},
        None,
    )

    records = [call.args[2] for call in mock_event.create_or_merge_sleep.call_args_list]
    assert [record.zone_offset for record in records] == [None, None]
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["action"] == "sleep_timezone_invalid"


@patch("app.services.providers.withings.data_247.event_record_service")
@patch("app.services.providers.withings.data_247.paginate")
def test_save_sleep_continues_on_row_error(mock_paginate: MagicMock, mock_event: MagicMock) -> None:
    d = _make_data_247()
    db = MagicMock()
    start = int(datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 16, 6, 0, tzinfo=timezone.utc).timestamp())
    row = {
        "id": 1,
        "startdate": start,
        "enddate": end,
        "data": {
            "deepsleepduration": 7200,
            "lightsleepduration": 14400,
            "remsleepduration": 5400,
            "wakeupduration": 1800,
            "sleep_efficiency": 0.85,
        },
    }
    # Two identical rows; first call raises, second succeeds
    mock_paginate.return_value = PaginatedResult(rows=[row, {**row, "id": 2}], envelope={})
    mock_event.create_or_merge_sleep.side_effect = [Exception("boom"), None]

    count = d.save_sleep(
        db, uuid4(), datetime(2024, 1, 15, tzinfo=timezone.utc), datetime(2024, 1, 16, tzinfo=timezone.utc)
    )
    assert count == 1
    db.rollback.assert_called()


def test_load_and_save_all_calls_each_domain() -> None:
    d = _make_data_247()
    db = MagicMock()
    d.save_measures = MagicMock(return_value=3)
    d.save_activity = MagicMock(return_value=2)
    d.save_sleep = MagicMock(return_value=1)
    result = d.load_and_save_all(
        db, uuid4(), datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 8, tzinfo=timezone.utc)
    )
    assert result == {"measures": 3, "activity": 2, "sleep": 1}


def test_load_and_save_all_runs_remaining_domains_then_raises() -> None:
    d = _make_data_247()
    db = MagicMock()
    d.save_measures = MagicMock(side_effect=Exception("boom"))
    d.save_activity = MagicMock(return_value=2)
    d.save_sleep = MagicMock(return_value=1)
    with pytest.raises(Exception, match="measures: boom"):
        d.load_and_save_all(
            db, uuid4(), datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 8, tzinfo=timezone.utc)
        )
    d.save_activity.assert_called_once()
    d.save_sleep.assert_called_once()
    db.rollback.assert_called()


@patch("app.services.providers.withings.data_247.event_record_service")
def test_external_sleep_uses_aggregate_durations_when_stages_are_null(mock_event: MagicMock) -> None:
    d = _make_data_247()
    start = int(datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 16, 6, 0, tzinfo=timezone.utc).timestamp())

    assert d._save_sleep_row(
        MagicMock(),
        uuid4(),
        {
            "startdate": start,
            "enddate": end,
            "data": {"total_timeinbed": 28800, "asleepduration": 27000},
        },
        None,
    )

    record = mock_event.create_or_merge_sleep.call_args.args[2]
    detail = mock_event.create_or_merge_sleep.call_args.args[3]
    assert record.duration_seconds == 28800
    assert detail.sleep_total_duration_minutes == 450
    assert detail.sleep_deep_minutes is None


def test_normalize_measures_skips_malformed_group() -> None:
    d = _make_data_247()
    groups = [
        {"measures": [{"value": 1, "type": 1, "unit": 0}]},  # no "date" → unparseable
        {"date": 1728000000, "measures": [{"type": 1, "unit": 0}]},  # measure missing "value"
        {"date": 1728000000, "measures": [{"value": 7500, "type": 1, "unit": -2}]},  # valid
    ]
    samples = d.normalize_measures(groups, uuid4())
    assert len(samples) == 1
    assert samples[0].value == Decimal("75.00")


@patch("app.services.providers.withings.data_247.paginate")
def test_save_activity_widens_the_local_day_window(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    d = Withings247Data(provider_name="withings", api_base_url="https://x", oauth=MagicMock())
    start = datetime(2018, 7, 2, tzinfo=timezone.utc)
    end = datetime(2018, 7, 2, 23, 0, tzinfo=timezone.utc)
    d.save_activity(MagicMock(), uuid4(), start, end)
    params = mock_paginate.call_args.kwargs["params"]
    assert mock_paginate.call_args.kwargs["service_path"] == "/v2/measure"
    assert mock_paginate.call_args.kwargs["action"] == "getactivity"
    assert mock_paginate.call_args.kwargs["list_key"] == "activities"
    assert params["data_fields"] == "steps,distance,calories,totalcalories"
    # A UTC window cannot express the requester's local day, so both edges widen.
    assert params["startdateymd"] == "2018-07-01"
    assert params["enddateymd"] == "2018-07-03"


@patch("app.services.providers.withings.data_247.paginate")
def test_save_sleep_widens_the_local_day_window(mock_paginate: MagicMock) -> None:
    mock_paginate.return_value = PaginatedResult(rows=[], envelope={})
    d = Withings247Data(provider_name="withings", api_base_url="https://x", oauth=MagicMock())
    start = datetime(2018, 7, 2, tzinfo=timezone.utc)
    end = datetime(2018, 7, 2, 23, 0, tzinfo=timezone.utc)
    d.save_sleep(MagicMock(), uuid4(), start, end)
    params = mock_paginate.call_args.kwargs["params"]
    assert mock_paginate.call_args.kwargs["service_path"] == "/v2/sleep"
    assert mock_paginate.call_args.kwargs["action"] == "getsummary"
    assert mock_paginate.call_args.kwargs["list_key"] == "series"
    assert params["data_fields"] == (
        "total_timeinbed,total_sleep_time,asleepduration,"
        "deepsleepduration,lightsleepduration,remsleepduration,wakeupduration,sleep_efficiency"
    )
    assert params["startdateymd"] == "2018-07-01"
    assert params["enddateymd"] == "2018-07-03"


@patch("app.services.providers.withings.data_247.event_record_service")
@patch("app.services.providers.withings.data_247.paginate")
def test_save_sleep_skips_unparseable_row(mock_paginate: MagicMock, mock_event: MagicMock) -> None:
    d = _make_data_247()
    db = MagicMock()
    start = int(datetime(2024, 1, 15, 22, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2024, 1, 16, 6, 0, tzinfo=timezone.utc).timestamp())
    valid = {"id": 1, "startdate": start, "enddate": end, "data": {"deepsleepduration": 7200}}
    malformed = {"id": 2, "enddate": end, "data": {}}  # no startdate → unparseable
    mock_paginate.return_value = PaginatedResult(rows=[malformed, valid], envelope={})

    count = d.save_sleep(
        db, uuid4(), datetime(2024, 1, 15, tzinfo=timezone.utc), datetime(2024, 1, 16, tzinfo=timezone.utc)
    )

    assert count == 1
    mock_event.create_or_merge_sleep.assert_called_once()


def test_sync_error_keeps_the_severity_of_a_handled_failure() -> None:
    error = WithingsDataSyncError({"measures": HTTPException(status_code=429, detail="throttled")})

    assert error.status_code == 429
    assert "measures" in error.detail


def test_sync_error_escalates_an_unclassified_failure() -> None:
    error = WithingsDataSyncError(
        {"measures": HTTPException(status_code=429, detail="throttled"), "sleep": RuntimeError("boom")},
    )

    assert error.status_code == 500


@patch("app.services.providers.withings.data_247.log_and_capture_error")
@patch("app.services.providers.withings.data_247.paginate")
def test_load_and_save_all_reports_each_failure_once(mock_paginate: MagicMock, mock_capture: MagicMock) -> None:
    d = _make_data_247()
    mock_paginate.side_effect = HTTPException(status_code=429, detail="throttled")

    with pytest.raises(WithingsDataSyncError) as exc_info:
        d.load_and_save_all(MagicMock(), uuid4())

    assert exc_info.value.status_code == 429
    assert set(exc_info.value.failures) == {"measures", "activity", "sleep"}
    mock_capture.assert_not_called()
