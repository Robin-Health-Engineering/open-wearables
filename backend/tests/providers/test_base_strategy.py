"""
Tests for BaseProviderStrategy.

Tests cover:
- Abstract properties and methods
- Concrete property implementations
- Repository initialization
- Component attributes
- Display names and icons
"""

from abc import ABC
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models import EventRecord, User
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.services.providers.apple.strategy import AppleStrategy
from app.services.providers.base_strategy import BaseProviderStrategy
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.polar.strategy import PolarStrategy
from app.services.providers.suunto.strategy import SuuntoStrategy
from app.services.providers.withings.strategy import WithingsStrategy


class TestBaseProviderStrategy:
    """Test suite for BaseProviderStrategy."""

    def test_is_abstract_class(self) -> None:
        """Should be an abstract base class."""
        # Assert
        assert issubclass(BaseProviderStrategy, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        """Should not allow direct instantiation."""
        # Act & Assert
        with pytest.raises(TypeError) as exc_info:
            BaseProviderStrategy()

        assert "Can't instantiate abstract class" in str(exc_info.value)

    def test_garmin_strategy_initializes_repositories(self) -> None:
        """Should initialize all repositories on GarminStrategy."""
        # Act
        strategy = GarminStrategy()

        # Assert
        assert isinstance(strategy.user_repo, UserRepository)
        assert strategy.user_repo.model == User
        assert isinstance(strategy.connection_repo, UserConnectionRepository)
        assert isinstance(strategy.workout_repo, EventRecordRepository)
        assert strategy.workout_repo.model == EventRecord

    def test_apple_strategy_initializes_repositories(self) -> None:
        """Should initialize all repositories on AppleStrategy."""
        # Act
        strategy = AppleStrategy()

        # Assert
        assert isinstance(strategy.user_repo, UserRepository)
        assert isinstance(strategy.connection_repo, UserConnectionRepository)
        assert isinstance(strategy.workout_repo, EventRecordRepository)

    def test_garmin_provider_name(self) -> None:
        """Should return correct provider name for Garmin."""
        # Act
        strategy = GarminStrategy()

        # Assert
        assert strategy.name == "garmin"

    def test_garmin_api_base_url(self) -> None:
        """Should return correct API base URL for Garmin."""
        # Act
        strategy = GarminStrategy()

        # Assert
        assert strategy.api_base_url == "https://apis.garmin.com"

    def test_garmin_display_name(self) -> None:
        """Should return capitalized display name for Garmin."""
        # Act
        strategy = GarminStrategy()

        # Assert
        assert strategy.display_name == "Garmin"

    def test_apple_display_name_override(self) -> None:
        """Should return custom display name for Apple."""
        # Act
        strategy = AppleStrategy()

        # Assert
        assert strategy.display_name == "Apple Health"

    def test_garmin_has_cloud_api(self) -> None:
        """Should return True for has_cloud_api when OAuth is present."""
        # Act
        strategy = GarminStrategy()

        # Assert
        assert strategy.has_cloud_api is True
        assert strategy.oauth is not None

    def test_apple_no_cloud_api(self) -> None:
        """Should return False for has_cloud_api when OAuth is absent."""
        # Act
        strategy = AppleStrategy()

        # Assert
        assert strategy.has_cloud_api is False
        assert strategy.oauth is None

    def test_withings_tears_down_subscriptions_on_disconnect(self) -> None:
        """A per-user provider overrides on_disconnect to revoke at the vendor."""
        strategy = WithingsStrategy()
        db, user_id = MagicMock(), uuid4()

        with patch.object(strategy.webhook_service, "remove_user") as remove_user:
            strategy.on_disconnect(db, user_id)

        remove_user.assert_called_once_with(db, user_id)

    def test_on_disconnect_is_a_no_op_by_default(self) -> None:
        """Providers holding no per-connection vendor state inherit a no-op."""
        assert AppleStrategy().on_disconnect(MagicMock(), uuid4()) is None

    def test_icon_url_generation(self) -> None:
        """Should generate correct icon URL path."""
        # Act
        garmin_strategy = GarminStrategy()
        apple_strategy = AppleStrategy()
        polar_strategy = PolarStrategy()
        suunto_strategy = SuuntoStrategy()

        # Assert
        assert garmin_strategy.icon_url == "/static/provider-icons/garmin.svg"
        assert apple_strategy.icon_url == "/static/provider-icons/apple.svg"
        assert polar_strategy.icon_url == "/static/provider-icons/polar.svg"
        assert suunto_strategy.icon_url == "/static/provider-icons/suunto.svg"
