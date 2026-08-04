from app.schemas.enums import SeriesType
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
        227: SeriesType.withings_metabolic_age,
    }
    assert expected == MEASURE_TYPE_MAP


def test_deferred_getmeas_types_are_recorded_and_never_mapped() -> None:
    assert DEFERRED_MEASURE_TYPES.keys().isdisjoint(MEASURE_TYPE_MAP)
    assert {12, 130, 140, 158, 159, 167, 196, 226}.issubset(DEFERRED_MEASURE_TYPES)
    assert "environmental temperature" in DEFERRED_MEASURE_TYPES[12]
    assert "device-aware mapping" in DEFERRED_MEASURE_TYPES[12]
    assert "left-foot Nerve Health Score" in DEFERRED_MEASURE_TYPES[158]
    assert "right-foot Nerve Health Score" in DEFERRED_MEASURE_TYPES[159]
    assert "source-contract conflict" in DEFERRED_MEASURE_TYPES[167]
    assert DEFERRED_MEASURE_TYPES[196] == "Nerve Response Score; no core series type"


def test_all_mapped_getmeas_series_are_declared_timeseries_coverage() -> None:
    assert set(MEASURE_TYPE_MAP.values()).issubset(TIMESERIES)
    assert SeriesType.basal_energy in TIMESERIES
