import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration

from app import __version__
from app.config import settings


def init_sentry() -> None:
    if settings.SENTRY_ENABLED:
        release = f"{__version__}+{settings.GIT_SHA[:12]}" if settings.GIT_SHA else __version__
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SENTRY_ENV,
            server_name=settings.SENTRY_SERVER_NAME,
            release=release,
            traces_sample_rate=settings.SENTRY_SAMPLES_RATE,
            # OFF, and this is a privacy control rather than a preference. sentry-python defaults
            # it to True, which attaches every frame's locals to an event — and on the Withings
            # partner surface a frame's locals ARE the signed payload. Measured on staging
            # (OW-BACKEND-B, 2026-09-15): a captured event carried the member's full name, street
            # address, email, phone number, birth date and weight, in plain text.
            #
            # That is the exact data this codebase takes care to keep out of `detail` and out of
            # the logs — see `_body_logging.describe_body` — undone by a default. The DPIA
            # classifies Sentry as carrying pseudonymous identifiers (cognito_sub, profile_id,
            # ow_user_id), not a home address, so the leak is a compliance problem and not only an
            # untidy one.
            #
            # The cost is real: local variables are genuinely useful in triage, and turning them
            # off made a 503 harder to diagnose on the very day this was found. The answer to that
            # is to log the ONE field that explains a failure — see `upstream_reason` — not to ship
            # every local to a third party and hope none of them is a member's address.
            include_local_variables=False,
            integrations=[
                CeleryIntegration(
                    monitor_beat_tasks=True,
                    propagate_traces=True,
                ),
            ],
        )
