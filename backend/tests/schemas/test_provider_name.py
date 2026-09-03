"""Tests for ProviderName enum."""

import pytest

from app.schemas.enums import DEFAULT_PROVIDER_PRIORITY, ProviderName


class TestProviderNameFromSourceString:
    """Test ProviderName.from_source_string() method."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            # Apple variations
            ("apple_health_sdk", ProviderName.APPLE),
            ("Apple Health", ProviderName.APPLE),
            ("APPLE_WATCH", ProviderName.APPLE),
            # Garmin variations
            ("garmin_connect", ProviderName.GARMIN),
            ("Garmin Fenix 7", ProviderName.GARMIN),
            ("GARMIN", ProviderName.GARMIN),
            # Polar variations
            ("polar_flow", ProviderName.POLAR),
            ("Polar Vantage", ProviderName.POLAR),
            ("POLAR", ProviderName.POLAR),
            # Suunto variations
            ("suunto_app", ProviderName.SUUNTO),
            ("Suunto 9", ProviderName.SUUNTO),
            ("SUUNTO", ProviderName.SUUNTO),
            # Whoop variations
            ("whoop_4.0", ProviderName.WHOOP),
            ("Whoop Strap", ProviderName.WHOOP),
            ("WHOOP", ProviderName.WHOOP),
            # Samsung variations
            ("samsung_health", ProviderName.SAMSUNG),
            ("samsung_health_sdk", ProviderName.SAMSUNG),
            ("Samsung Galaxy Watch", ProviderName.SAMSUNG),
            ("SAMSUNG", ProviderName.SAMSUNG),
            # Oura variations
            ("oura_ring", ProviderName.OURA),
            ("Oura Gen 3", ProviderName.OURA),
            ("OURA", ProviderName.OURA),
            # Fitbit variations
            ("fitbit", ProviderName.FITBIT),
            ("Fitbit", ProviderName.FITBIT),
            ("FITBIT", ProviderName.FITBIT),
            ("fitbit_sense", ProviderName.FITBIT),
            # Withings variations
            ("withings", ProviderName.WITHINGS),
            ("Withings", ProviderName.WITHINGS),
            ("withings_health_mate", ProviderName.WITHINGS),
            # Withings relayed through Apple Health resolves to APPLE, because
            # from_source_string iterates the enum in DECLARATION order and APPLE is
            # declared first. That is the correct answer (the writer really is Apple
            # Health) but it depends on enum ordering, so it is pinned here: moving
            # WITHINGS above APPLE in the enum would silently re-attribute every
            # relayed sample and split its data_source rows.
            ("Withings via Apple Health", ProviderName.APPLE),
            # Unknown cases
            ("unknown_device", ProviderName.UNKNOWN),
            ("", ProviderName.UNKNOWN),
        ],
    )
    def test_from_source_string_inference(self, source: str, expected: ProviderName) -> None:
        """Should correctly infer provider from source string."""
        assert ProviderName.from_source_string(source) == expected

    def test_from_source_string_none(self) -> None:
        """Should return UNKNOWN for None source."""
        assert ProviderName.from_source_string(None) == ProviderName.UNKNOWN

    def test_from_source_string_case_insensitive(self) -> None:
        """Should be case-insensitive."""
        assert ProviderName.from_source_string("ApPlE") == ProviderName.APPLE
        assert ProviderName.from_source_string("gArMiN") == ProviderName.GARMIN

    def test_from_source_string_partial_match(self) -> None:
        """Should match provider name anywhere in the string."""
        assert ProviderName.from_source_string("my_apple_watch_data") == ProviderName.APPLE
        assert ProviderName.from_source_string("data_from_garmin_device") == ProviderName.GARMIN


class TestProviderPriorityDefaults:
    """DEFAULT_PROVIDER_PRIORITY must be deterministic for providers we ship ourselves."""

    def test_withings_has_an_explicit_default_priority(self) -> None:
        """Withings must not fall back to the insertion-order-dependent next priority.

        ProviderPriorityRepository.ensure_provider_exists() does
        DEFAULT_PROVIDER_PRIORITY.get(provider, self.get_next_priority(db)), so a missing
        entry yields max(priority)+1 - a value that depends on how many providers happen
        to be in the table already, and therefore differs between environments.
        """
        assert ProviderName.WITHINGS in DEFAULT_PROVIDER_PRIORITY

    def test_default_priorities_are_unique(self) -> None:
        """Two providers sharing a rank makes source-winner selection order-dependent."""
        values = list(DEFAULT_PROVIDER_PRIORITY.values())
        assert len(values) == len(set(values)), f"duplicate priorities: {values}"
