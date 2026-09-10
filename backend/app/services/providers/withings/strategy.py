import logging
from uuid import UUID

from app.config import settings
from app.database import DbSession
from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.withings.connections import is_device_connection
from app.services.providers.withings.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.withings.data_247 import Withings247Data
from app.services.providers.withings.end_program import end_partner_program
from app.services.providers.withings.notify_service import WithingsNotifyService
from app.services.providers.withings.oauth import WithingsOAuth
from app.services.providers.withings.webhook_handler import WithingsWebhookHandler
from app.services.providers.withings.workouts import WithingsWorkouts
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class WithingsEndProgramError(RuntimeError):
    """Raised only to give Sentry a typed, groupable event — never propagated to a caller."""

    def __init__(self, *, count: int) -> None:
        super().__init__(f"Withings endpartnerprogram failed for {count} device(s)")


class WithingsStrategy(BaseProviderStrategy):
    # Narrowed from the base's optional: Withings always manages its own subscriptions.
    webhook_service: WithingsNotifyService

    def __init__(self) -> None:
        super().__init__()
        self.oauth = WithingsOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.data_247 = Withings247Data(
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.workouts = WithingsWorkouts(
            workout_repo=self.workout_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.webhooks = WithingsWebhookHandler(
            data_247=self.data_247,
            workouts=self.workouts,
            default_live_sync_mode=self.default_live_sync_mode,
        )
        self.webhook_service = WithingsNotifyService(
            connection_repo=self.connection_repo,
            oauth=self.oauth,
            default_live_sync_mode=self.default_live_sync_mode,
        )

    @property
    def name(self) -> str:
        return "withings"

    @property
    def api_base_url(self) -> str:
        return "https://wbsapi.withings.net"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            rest_pull=True,
            webhook_ping=True,
            webhook_registration_api=True,
            webhook_subscription_per_user=True,
        )

    def on_disconnect(self, db: DbSession, user_id: UUID, *, connection_id: UUID | None = None) -> None:
        """Vendor-side teardown before the tokens are cleared.

        Two things, and they are not the same kind of thing.

        Notify subscriptions are ours to prune and best-effort: failing leaves a subscription
        pointing at a callback that will stop resolving to anything, which is untidy and free.

        Ending the cellular programme is not. A device we shipped sits on a plan Withings bills
        us for, and revoking locally without telling them leaves it transmitting and charging —
        so a failure here is reported rather than logged and forgotten. It still does not block
        the disconnect: a member must be able to remove a device while Withings is unreachable,
        and a plan we are still paying for is a smaller harm than a member who cannot leave.
        """
        self.webhook_service.remove_user(db, user_id, connection_id=connection_id)
        self._end_cellular_program(db, user_id, connection_id)

    def _end_cellular_program(self, db: DbSession, user_id: UUID, connection_id: UUID | None) -> None:
        """Tell Withings the programme is over for whatever devices this connection carries.

        Only for a connection WE provisioned — a member's own linked account has no device of
        ours on it, and calling for one would be asking Withings to end a programme that does
        not exist.

        The MACs come from the caller, not from here: they live in robin-backend's DynamoDB
        alongside the order, because that is commerce rather than health data, and Open Wearables
        must never call back into robin-backend. Until that proxy passes them (robin-backend's
        WithingsDeviceOrder work), this resolves an empty list and calls nothing — which is the
        same no-op it correctly performs for a member-linked connection.
        """
        if connection_id is None or not is_device_connection(db, connection_id):
            return

        if not settings.withings_client_id or not settings.withings_client_secret:
            log_structured(
                logger,
                "error",
                "Cannot end the Withings cellular programme: client credentials are not configured",
                provider=self.name,
                task="end_partner_program",
                user_id=str(user_id),
            )
            return

        mac_addresses = self._device_mac_addresses(db, connection_id)
        if not mac_addresses:
            return

        results = end_partner_program(
            client_id=settings.withings_client_id,
            client_secret=settings.withings_client_secret.get_secret_value(),
            mac_addresses=mac_addresses,
            api_base_url=self.api_base_url,
        )
        failed = [r for r in results if not r.ok]
        if failed:
            # Captured, not just logged: every one of these is a cellular plan still billing.
            log_and_capture_error(
                WithingsEndProgramError(count=len(failed)),
                logger,
                "Withings cellular programme could not be ended for every device",
                extra={
                    "provider": self.name,
                    "user_id": str(user_id),
                    "connection_id": str(connection_id),
                    "failed": len(failed),
                    "total": len(results),
                },
            )

    def _device_mac_addresses(self, db: DbSession, connection_id: UUID) -> list[str]:
        """The MACs to end the programme for.

        Empty today, and that is the honest state rather than a stub: OW does not store MACs,
        by design (nothing else here reads one), and the disconnect proxy that will supply them
        is robin-backend's to build. Returning nothing means "call nothing", which is exactly
        right until it does.
        """
        return []

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            health_scores=HEALTH_SCORES,
        )
