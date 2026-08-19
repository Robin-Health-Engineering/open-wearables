from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal
from uuid import UUID

from celery import current_app as celery_app

from app.database import DbSession
from app.models import EventRecord, User
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.provider_settings_repository import ProviderSettingsRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import LiveSyncMode, resolve_live_sync_mode
from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory
from app.services.providers.templates.base_247_data import Base247DataTemplate
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.templates.base_webhook_service import BaseWebhookService
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.utils.exceptions import UnsupportedProviderError


@dataclass
class HistoricalSyncResult:
    """Result of dispatching a historical sync task."""

    task_id: str
    method: Literal["pull_api", "webhook_backfill"]
    message: str
    days: int | None
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class ProviderCoverage:
    """Declares what data a provider actually delivers, grouped by API layer.

    timeseries:             SeriesType values available via /timeseries endpoint
    workout_fields:         EventRecordDetail fields populated in workout records
    sleep_fields:           EventRecordDetail fields populated in sleep records
    menstrual_cycle_fields: EventRecordDetail fields populated in menstrual-cycle records
    health_scores:          HealthScoreCategory values produced by this provider

    Define the frozensets in the provider's coverage.py and assign here in
    strategy.py — keeps implementation files free of metadata declarations.
    A provider that delivers no data for a dimension simply omits it (the
    default empty frozenset), so no empty placeholders are needed elsewhere.
    """

    timeseries: frozenset[SeriesType] = field(default_factory=frozenset)
    workout_fields: frozenset[str] = field(default_factory=frozenset)
    sleep_fields: frozenset[str] = field(default_factory=frozenset)
    menstrual_cycle_fields: frozenset[str] = field(default_factory=frozenset)
    health_scores: frozenset[HealthScoreCategory] = field(default_factory=frozenset)


class WebhookSubscriptionOwner(StrEnum):
    """Identify who owns and creates a provider webhook subscription."""

    APPLICATION = "application"
    """One application-level registration covers every user."""

    USER = "user"
    """Each connection owns subscriptions created with its bearer token."""


@dataclass(frozen=True)
class ProviderCapabilities:
    """Fine-grained capability flags for a provider's data delivery model.

    Attributes
    ----------
    rest_pull:
        Provider exposes a REST API that can be polled for historical or
        recent data (``load_data()`` / ``get_workouts()``).
    client_sdk:
        Data arrives via our mobile SDK endpoint (Samsung Health, Google
        Health Connect, Apple HealthKit).
    file_import:
        Data arrives as a file export from the user's device (Apple Health
        XML). May coexist with ``client_sdk`` for Apple.
    webhook_callback: [request & push]
        We initiate a REST request to start a data export; the provider
        delivers the result to our webhook asynchronously.
        Used for historical backfill. Currently only Garmin.
    webhook_stream [push full-payload]:
        Provider pushes the complete data payload to our webhook inline.
        Live sync runs exclusively from webhooks;
    webhook_ping [notify & pull]:
        Provider sends a lightweight ping to our webhook.
        Actual data must be fetched via REST (``rest_pull`` must be
        ``True``). Oura, Strava, Fitbit, Polar.
    webhook_subscription_owner:
        Who owns the provider's webhook subscriptions, or ``None`` when they
        cannot be managed programmatically. Application-owned subscriptions
        register once; user-owned subscriptions reconcile each active connection.
        When set, switching live-sync mode triggers the
        ``register_provider_webhooks`` Celery task.
        ``APPLICATION``: Polar, Oura, Strava, Google. ``USER``: Withings.
    webhook_inbound_secret:
        Provider signs inbound webhook payloads with HMAC; the signing
        secret is returned by the registration API (not pre-configured in
        env vars) and is stored in ``provider_settings.webhook_secret``.
        Requires ``webhook_subscription_owner`` to be set, since the secret comes
        back from the registration call. Currently: Polar.
    max_historical_days:
        Hard upper limit on how far back the provider allows data to be
        fetched. ``None`` means no known limit. Garmin: 30 days.
    """

    rest_pull: bool = False
    client_sdk: bool = False
    file_import: bool = False
    webhook_callback: bool = False
    webhook_stream: bool = False
    webhook_ping: bool = False
    webhook_subscription_owner: WebhookSubscriptionOwner | None = None
    webhook_inbound_secret: bool = False
    max_historical_days: int | None = None

    def __post_init__(self) -> None:
        if self.webhook_stream and self.webhook_ping:
            raise ValueError("webhook_stream and webhook_ping are mutually exclusive")
        if self.webhook_ping and not self.rest_pull:
            raise ValueError("webhook_ping requires rest_pull=True (data must be fetched via REST after the ping)")
        # The secret is handed back by the registration call, so it presupposes that
        # subscriptions are registered programmatically at all — not that the application
        # rather than the user owns them.
        if self.webhook_inbound_secret and self.webhook_subscription_owner is None:
            raise ValueError("webhook_inbound_secret requires webhook_subscription_owner to be set")


class BaseProviderStrategy(ABC):
    """Abstract base class for all fitness data providers."""

    def __init__(self):
        """Initialize shared repositories used by all provider components."""
        self.user_repo = UserRepository(User)
        self.connection_repo = UserConnectionRepository()
        self.workout_repo = EventRecordRepository(EventRecord)

        # Components should be initialized by subclasses
        self.oauth: BaseOAuthTemplate | None = None
        self.workouts: BaseWorkoutsTemplate | None = None
        self.data_247: Base247DataTemplate | None = None
        self.webhooks: BaseWebhookHandler | None = None
        self.webhook_service: BaseWebhookService | None = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Returns the unique name of the provider (e.g., 'garmin', 'suunto')."""

    @property
    @abstractmethod
    def api_base_url(self) -> str:
        """Returns the base URL for the provider's API."""

    @property
    def api_version(self) -> str:
        """API version string (e.g. 'v3'). Override in providers that version their API."""
        return ""

    @property
    def api_current_url(self) -> str:
        """Versioned API base URL. Override or let the default derive from api_base_url + api_version."""
        if self.api_version:
            return f"{self.api_base_url}/api/{self.api_version}"
        return self.api_base_url

    @property
    def coverage(self) -> ProviderCoverage:
        """Declares what data this provider delivers across all API layers.

        Override in each provider strategy by returning a ProviderCoverage built
        from constants defined in the provider's coverage.py.
        """
        return ProviderCoverage()

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Declares the data delivery capabilities of this provider.

        Each concrete strategy must override this to accurately reflect what
        data delivery modes the provider supports. The unified webhook router
        and sync scheduler use this to decide how to handle the provider.

        Example::

            @property
            def capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(
                    rest_pull=True,
                    webhook_ping=True,
                )
        """

    def start_historical_sync(self, user_id: UUID, days: int) -> HistoricalSyncResult:
        """Dispatch an async historical data sync.

        Default implementation works for pull-based providers. Override for
        providers that use a different mechanism (e.g. Garmin webhook backfill).

        Raises UnsupportedProviderError for providers that don't support historical sync.
        """
        if not self.capabilities.rest_pull:
            raise UnsupportedProviderError(self.name, "historical sync")

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)

        task = celery_app.send_task(
            "app.integrations.celery.tasks.sync_vendor_data_task.sync_vendor_data",
            kwargs={
                "user_id": str(user_id),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "providers": [self.name],
                "is_historical": True,
            },
        )

        return HistoricalSyncResult(
            task_id=task.id,
            method="pull_api",
            message=f"Historical sync queued for {days} days of {self.name} data.",
            days=days,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )

    @property
    def display_name(self) -> str:
        """Returns the display name of the provider (e.g., 'Garmin', 'Apple Health')."""
        return self.name.capitalize()

    @property
    def has_cloud_api(self) -> bool:
        """Returns True if provider uses cloud OAuth API."""
        return self.oauth is not None

    @property
    def per_user_webhook_service(self) -> BaseWebhookService | None:
        """This provider's webhook service, only when subscriptions are user-owned.

        Callers that tear down one connection's subscriptions (disconnect, data
        purge) use this to skip providers whose subscriptions are application-owned
        and would outlive the connection being removed.
        """
        if self.capabilities.webhook_subscription_owner != WebhookSubscriptionOwner.USER:
            return None
        return self.webhook_service

    @property
    def live_sync_configurable(self) -> bool:
        """True when the admin can choose between pull and webhook live sync.

        Requires rest_pull (periodic fallback exists) plus at least one
        webhook delivery mode (webhook_stream or webhook_ping).
        """
        caps = self.capabilities
        return caps.rest_pull and (caps.webhook_stream or caps.webhook_ping)

    @property
    def default_live_sync_mode(self) -> LiveSyncMode | None:
        """Derive the default live_sync_mode from this provider's capabilities.

        Rules (in priority order):
        - rest_pull → PULL (REST polling is the safe default even if webhooks exist)
        - client_sdk only → None (no server-side sync)
        - webhook_* only, no rest_pull → WEBHOOK
        """
        caps = self.capabilities
        if caps.rest_pull:
            return LiveSyncMode.PULL
        if caps.client_sdk:
            return None
        if caps.webhook_ping or caps.webhook_stream:
            return LiveSyncMode.WEBHOOK
        return None

    def effective_live_sync_mode(self, db: DbSession) -> LiveSyncMode | None:
        """The admin override from provider settings, else this provider's default.

        ``None`` means this provider has no server-side live-sync mode.
        """
        return resolve_live_sync_mode(
            ProviderSettingsRepository().get_live_sync_mode(db, self.name),
            self.default_live_sync_mode,
        )

    @property
    def icon_url(self) -> str:
        """Returns the URL path to the provider's icon."""
        return f"/static/provider-icons/{self.name}.svg"
