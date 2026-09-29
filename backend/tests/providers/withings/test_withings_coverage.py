from app.schemas.enums import SeriesType, get_series_type_id, get_series_type_unit
from app.schemas.enums.aggregation_method import AggregationMethod, get_aggregation_method
from app.schemas.enums.series_types import SERIES_TYPE_CATEGORY_BY_ENUM, SERIES_TYPE_DEFINITIONS
from app.services.providers.withings.coverage import DEFERRED_MEASURE_TYPES, MEASURE_TYPE_MAP, TIMESERIES


def test_getmeas_mapping_is_limited_to_core_semantic_matches() -> None:
    expected = {
        1: SeriesType.weight,
        4: SeriesType.height,
        5: SeriesType.lean_body_mass,
        6: SeriesType.body_fat_percentage,
        8: SeriesType.body_fat_mass,
        9: SeriesType.blood_pressure_diastolic,
        10: SeriesType.blood_pressure_systolic,
        11: SeriesType.heart_rate,
        54: SeriesType.oxygen_saturation,
        71: SeriesType.body_temperature,
        73: SeriesType.skin_temperature,
        76: SeriesType.skeletal_muscle_mass,
        77: SeriesType.body_water_mass,
        88: SeriesType.bone_mass,
        91: SeriesType.withings_pulse_wave_velocity,
        119: SeriesType.blood_glucose,
        123: SeriesType.vo2_max,
        155: SeriesType.cardiovascular_age,
        170: SeriesType.withings_visceral_fat,
        226: SeriesType.withings_basal_metabolic_rate,
        227: SeriesType.withings_metabolic_age,
    }
    assert expected == MEASURE_TYPE_MAP


def test_deferred_getmeas_types_are_recorded_and_never_mapped() -> None:
    assert DEFERRED_MEASURE_TYPES.keys().isdisjoint(MEASURE_TYPE_MAP)
    assert {12, 130, 140, 158, 159, 167, 196}.issubset(DEFERRED_MEASURE_TYPES)
    assert 170 not in DEFERRED_MEASURE_TYPES
    assert 226 not in DEFERRED_MEASURE_TYPES
    assert "environmental temperature" in DEFERRED_MEASURE_TYPES[12]
    assert "device-aware mapping" in DEFERRED_MEASURE_TYPES[12]
    assert "left-foot Nerve Health Score" in DEFERRED_MEASURE_TYPES[158]
    assert "right-foot Nerve Health Score" in DEFERRED_MEASURE_TYPES[159]
    assert "source-contract conflict" in DEFERRED_MEASURE_TYPES[167]
    assert DEFERRED_MEASURE_TYPES[196] == "Nerve Response Score; no core series type"


def test_all_mapped_getmeas_series_are_declared_timeseries_coverage() -> None:
    assert set(MEASURE_TYPE_MAP.values()).issubset(TIMESERIES)
    assert SeriesType.basal_energy in TIMESERIES


def test_withings_series_use_their_canonical_categories() -> None:
    assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.body_water_mass] == "Body Composition"
    assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.bone_mass] == "Body Composition"
    assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.withings_pulse_wave_velocity] == "Provider-Specific"
    assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.withings_metabolic_age] == "Provider-Specific"
    assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.withings_visceral_fat] == "Provider-Specific"
    assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.withings_basal_metabolic_rate] == "Provider-Specific"


def test_visceral_fat_and_bmr_live_in_the_robin_fork_id_range() -> None:
    # Not 242/243: upstream owns the Withings block (240-259), and the seed upserts by id, so an
    # upstream series landing on the same id would silently relabel our stored rows.
    assert get_series_type_id(SeriesType.withings_visceral_fat) == 900
    assert get_series_type_id(SeriesType.withings_basal_metabolic_rate) == 901
    assert get_series_type_unit(SeriesType.withings_visceral_fat) == "score"
    assert get_series_type_unit(SeriesType.withings_basal_metabolic_rate) == "kcal"
    assert get_aggregation_method(SeriesType.withings_visceral_fat) == AggregationMethod.AVG
    assert get_aggregation_method(SeriesType.withings_basal_metabolic_rate) == AggregationMethod.AVG


def test_the_robin_fork_id_range_holds_only_fork_series() -> None:
    # Fails loudly if an upstream merge ever puts one of its series in the fork's range.
    in_range = {type_id: enum for type_id, enum, _ in SERIES_TYPE_DEFINITIONS if 900 <= type_id < 1000}
    assert in_range == {
        900: SeriesType.withings_visceral_fat,
        901: SeriesType.withings_basal_metabolic_rate,
    }
