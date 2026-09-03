"""Provision a Withings SDK account for a member and persist the result.

Ties together the three steps that must succeed as one: ``createuser``, the code exchange,
and storing what came back. Split across callers they can half-succeed, and a half-succeeded
provisioning is the bad case — a connection with no ``csrf_token`` looks healthy and then
cannot open a WebView.

One connection per member per provider is enforced by ``user_connection``'s unique
``(user_id, provider)`` index, so a member cannot hold both a personally-linked Withings
account and an SDK-provisioned one. Where both happen, **the SDK account wins**: provisioning
overwrites the tokens on the existing row (Francesco's call). The consequence is real and the
UI must say so before provisioning — a personally-linked account stops syncing, keeping its
history but gaining nothing new.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.database import DbSession
from app.models.withings_sdk_account import WithingsSdkAccount
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.schemas.model_crud.user_management import UserConnectionCreate
from app.services.outgoing_webhooks.events import on_connection_created
from app.services.providers.withings.sdk_users import SdkTokens, create_sdk_user, exchange_sdk_code
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


def _upsert_sdk_account(db: DbSession, *, connection_id: UUID, external_id: str, csrf_token: str) -> WithingsSdkAccount:
    """Write the SDK-only state, keyed one-to-one with the connection.

    Updated rather than replaced on re-provisioning: ``csrf_token`` is reissued with every
    token refresh, so this row is rewritten often and its identity must not churn.
    """
    existing = db.query(WithingsSdkAccount).filter(WithingsSdkAccount.user_connection_id == connection_id).one_or_none()
    now = datetime.now(timezone.utc)
    if existing:
        existing.external_id = external_id
        existing.csrf_token = csrf_token
        existing.updated_at = now
        db.flush()
        return existing

    account = WithingsSdkAccount(
        id=uuid4(),
        user_connection_id=connection_id,
        external_id=external_id,
        csrf_token=csrf_token,
        updated_at=now,
    )
    db.add(account)
    db.flush()
    return account


def provision_sdk_account(
    db: DbSession,
    *,
    user_id: UUID,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    external_id: str,
    email: str,
    shortname: str,
    birthdate: int,
    gender: int,
    weight_kg: float,
    height_m: float,
    preflang: str,
    timezone_name: str,
    mailingpref: int,
    api_base_url: str | None = None,
) -> WithingsSdkAccount:
    """Create the Withings account, exchange its code, and store both halves.

    Returns the SDK account row. The caller gets ``csrf_token`` from it, which together with
    a live access token is what opens the hosted setup and settings WebViews.
    """
    kwargs = {"api_base_url": api_base_url} if api_base_url else {}

    sdk_user = create_sdk_user(
        client_id=client_id,
        client_secret=client_secret,
        external_id=external_id,
        email=email,
        shortname=shortname,
        birthdate=birthdate,
        gender=gender,
        weight_kg=weight_kg,
        height_m=height_m,
        preflang=preflang,
        timezone=timezone_name,
        mailingpref=mailingpref,
        **kwargs,
    )

    tokens: SdkTokens = exchange_sdk_code(
        client_id=client_id,
        client_secret=client_secret,
        code=sdk_user.code,
        redirect_uri=redirect_uri,
        **kwargs,
    )

    repo = UserConnectionRepository()
    provider = ProviderName.WITHINGS.value
    existing = repo.get_by_user_and_provider(db, user_id, provider)

    if existing:
        # SDK wins. This may be overwriting a personally-linked account — see the module
        # docstring; the warning belongs in the UI, not in a silent branch here.
        log_structured(
            logger,
            "warning" if existing.provider_user_id != tokens.userid else "info",
            "Withings SDK provisioning is replacing an existing connection",
            provider=provider,
            task="provision_sdk_account",
            user_id=str(user_id),
            previous_provider_user_id=existing.provider_user_id,
            new_provider_user_id=tokens.userid,
        )
        repo.update_connection_info(
            db,
            existing,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            provider_user_id=tokens.userid,
            provider_username=None,
            scope=tokens.scope,
        )
        connection = existing
    else:
        connection = repo.create(
            db,
            UserConnectionCreate(
                user_id=user_id,
                provider=provider,
                provider_user_id=tokens.userid,
                provider_username=None,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in),
                scope=tokens.scope,
            ),
        )
        on_connection_created(
            user_id=user_id,
            provider=provider,
            connection_id=connection.id,
            connected_at=connection.created_at.isoformat(),
        )

    account = _upsert_sdk_account(
        db,
        connection_id=connection.id,
        external_id=sdk_user.external_id,
        csrf_token=tokens.csrf_token,
    )
    db.commit()

    log_structured(
        logger,
        "info",
        "Withings SDK account provisioned",
        provider=provider,
        task="provision_sdk_account",
        user_id=str(user_id),
        external_id=sdk_user.external_id,
    )
    return account
