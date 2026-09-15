"""Provision a Withings SDK account for a member and persist the result.

Ties together the three steps that must succeed as one: ``createuser``, the code exchange,
and storing what came back. Split across callers they can half-succeed, and a half-succeeded
provisioning is the bad case — a connection with no ``csrf_token`` looks healthy and then
cannot open a WebView.

A member may hold up to TWO Withings connections, and provisioning adds one rather than
replacing what is there. Two facts force it: Withings creates an account on every provisioning
path they offer, and a cellular device cannot be activated onto an account the partner did not
create. So a member who has linked their own Withings account and is then shipped a device holds
both.

**Two, not one per order.** Withings confirmed on 2026-09-15 that an account created by
``createuserorder`` is REUSED by that member's later orders — the second device ships to the
account the first one made. robin-backend therefore sends a stable ``external_id`` (the bare
profile id), and the repeat provisioning that produces must REUSE the connection it finds rather
than failing the unique index. That is what ``_store_provisioned_account`` does below, and it is
the difference between a member's second order shipping and a second order 502-ing after Withings
have already placed it.

This used to overwrite instead, because the unique index was ``(user_id, provider)`` and there
was nowhere to put a second row — which meant shipping someone a blood-pressure monitor
silently stopped their own scale and watch from syncing. The index now includes
``provider_user_id``, so the personal account and the one we created coexist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from celery import current_app as celery_app
from sqlalchemy.exc import IntegrityError

from app.database import DbSession
from app.integrations.celery.task_names import SYNC_PROVIDER_USER_SUBSCRIPTION_TASK
from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName
from app.schemas.model_crud.user_management import UserConnectionCreate
from app.schemas.providers.withings.dropshipment import DropshipOrder, DropshipOrderResult
from app.services.outgoing_webhooks.events import on_connection_created
from app.services.providers.withings.dropshipment import WithingsDropshipmentError, create_user_order
from app.services.providers.withings.sdk_users import (
    SdkTokens,
    WithingsSdkUserError,
    create_sdk_user,
    exchange_sdk_code,
)
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CellularProvisioning:
    """What a cellular provisioning produced, split by which system owns it.

    ``account`` is ours: a Withings account, its connection and its tokens. ``orders`` are
    robin-backend's — order ids and shipment status, which this fork deliberately does not
    store. Returned together because one signed call creates both, and separated here so the
    caller cannot accidentally persist the half that does not belong in Postgres.
    """

    account: WithingsSdkAccount
    orders: list[DropshipOrderResult]


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


def _schedule_subscription_sync(user_id: UUID) -> None:
    """Ask the webhook worker to reconcile this member's Withings subscriptions.

    **The gap this closes.** ``WithingsNotifyService`` has been complete since the Notify work
    landed, but nothing on the PROVISIONING path enqueued it. The two existing enqueue sites are
    the OAuth callback (``oauth.py:178``) and the ``register_subscriptions`` fan-out
    (``notify_service.py:82``) — and a provisioned account never goes through OAuth, while the
    fan-out is **not periodic**: it has no ``beat_schedule`` entry, and runs only when an operator
    hits the admin route or changes the live-sync mode. So a member shipped a cellular device had
    a connection, live tokens and no Notify subscription: their scale would upload to Withings and
    nothing would ever tell us a measurement existed, until an operator did something unrelated.
    Nothing reported it either, because a missing subscription looks exactly like a member who has
    not stepped on the scale.

    Fire-and-forget, and unconditional on the gating the OAuth callback applies. The task itself
    reads the configured live-sync mode and returns ``skipped`` when it is not WEBHOOK, so
    repeating that check here would be a second copy of a decision that can change underneath it —
    and getting the copy wrong fails silent in the direction of no subscription at all.

    Never raises. A provisioning that succeeded must not be reported as failed because a broker
    was briefly unreachable: the account exists, the device is shipping, and a missed subscription
    is recoverable by the next fan-out. The failure is logged rather than swallowed quietly.
    """
    try:
        celery_app.send_task(
            SYNC_PROVIDER_USER_SUBSCRIPTION_TASK,
            args=[ProviderName.WITHINGS.value, str(user_id)],
            queue="webhook_sync",
        )
    except Exception as e:
        log_structured(
            logger,
            "error",
            "Withings provisioning could not schedule the subscription sync",
            provider=ProviderName.WITHINGS.value,
            task="provision_subscription_sync",
            user_id=str(user_id),
            error=str(e),
        )


def _store_provisioned_account(
    db: DbSession,
    *,
    user_id: UUID,
    external_id: str,
    tokens: SdkTokens,
    store_error: type[WithingsSdkUserError] | type[WithingsDropshipmentError] = WithingsSdkUserError,
) -> WithingsSdkAccount:
    """Persist a provisioned Withings account: its connection, its SDK row, one commit.

    Shared by both provisioning paths — the SDK's ``createuser`` and cellular's
    ``createuserorder`` — because what they do with the result is identical once the tokens are
    in hand. Only how the account was created differs, and that is the caller's half.

    ``store_error`` is which exception a failed store raises, and it is the caller's to choose
    because the two paths leave DIFFERENT things stranded upstream. An SDK failure strands an
    account; a cellular one strands an account AND a placed order, which is why
    ``WithingsDropshipmentError`` exists as a distinct type. Raising the SDK error on the
    cellular path would mean a caller catching the dropshipment error never sees the one failure
    it most needs to hear about.
    """
    provider = ProviderName.WITHINGS.value
    # Never REPLACE a different account, but do REUSE the same one. A member can hold two Withings
    # accounts — their own, linked through consumer OAuth, and the one we created — and those two
    # must coexist, which is what the three-column unique index is for.
    #
    # This branch used to find any existing Withings connection and overwrite it, which meant
    # shipping someone a blood-pressure monitor silently stopped their own scale and watch from
    # syncing.
    #
    # It then swung the other way and created unconditionally, on the reading that every order
    # yields a NEW account. Withings say otherwise (2026-09-15): a second order ships to the
    # account the first one created, so a repeat provisioning hands back the SAME provider_user_id
    # and an unconditional insert violates (user_id, provider, provider_user_id) — after
    # createuserorder has already placed the order. Refusing there would mean a member's second
    # device is paid for, shipped by Withings, and unknown to us.
    #
    # So: look for the connection this account already has, and refresh its tokens if it is there.
    # Matching on provider_user_id is what makes that safe — it is the Withings account's own id,
    # so this can only ever touch the connection for the very account Withings just handed back,
    # never the member's personal one.
    #
    # ONE TRANSACTION, and NOT through ``UserConnectionRepository.create``, which is the whole
    # point of building the row by hand here. ``CrudRepository.create`` COMMITS
    # (``repositories.py:28``), so the connection became durable BEFORE the SDK-account row was
    # written — and a UNIQUE violation on ``withings_sdk_account.external_id`` then rolled back
    # only the uncommitted half. What survived was a committed connection holding live Withings
    # tokens with no SDK row and therefore no ``csrf_token``: exactly the "half-succeeded
    # provisioning" this module's docstring calls the bad case, reached by an ordinary retry
    # rather than by anything exotic (Lucas, #12).
    #
    # Adding both rows and committing once makes that state unreachable: either the member has a
    # connection WITH its csrf_token, or they have neither and the caller gets a clean error.
    # What this does NOT fix is the Withings side — the account and any order it placed are
    # already real by the time we get here, and no database transaction can undo them. That is
    # what ``store_error`` is for, and why the caller answers 409 rather than retrying.
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in)
    existing = (
        db.query(UserConnection)
        .filter(
            UserConnection.user_id == user_id,
            UserConnection.provider == provider,
            UserConnection.provider_user_id == tokens.userid,
        )
        .one_or_none()
    )
    reused = existing is not None
    if existing is not None:
        # REUSE means the same account, under the same name, and an account that is OURS. Two
        # ways that can fail, and both refuse rather than rewrite:
        #
        # * NO `withings_sdk_account` row — per `withings/connections.py` that is the
        #   discriminator for the opposite thing: "Row present means we created the account; row
        #   absent means the member did." Falling through would overwrite the member's own tokens
        #   with partner-minted ones and then attach an SDK row, silently reclassifying their
        #   account as one we provisioned — the incident this module exists to prevent, reached
        #   through a different door. It is also the case `user_connection.py` keeps the
        #   three-column index for ("the guard if Withings ever ADOPTS an existing account …
        #   fails loudly here"), and before the reuse branch the unconditional insert delivered
        #   that loud failure. It has to stay loud (Lucas, #13).
        # * a DIFFERENT external_id — the two identifiers have come apart. `external_id` is the
        #   join robin-backend resolves an account by; silently moving it breaks that link with no
        #   error anywhere, which is strictly worse than a 409 someone can reconcile.
        held = db.query(WithingsSdkAccount).filter(WithingsSdkAccount.user_connection_id == existing.id).one_or_none()
        if held is None:
            raise store_error(
                detail="this Withings account is the member's own linked connection, not one we provisioned",
                already_exists=True,
            )
        if held.external_id != external_id:
            raise store_error(
                detail="this Withings account is already provisioned under a different external_id",
                already_exists=True,
            )
        # The tokens are new — the code exchange just minted them — and the old ones are already
        # dead, so writing them is the point of coming back here rather than an optimisation.
        existing.access_token = tokens.access_token
        existing.refresh_token = tokens.refresh_token
        existing.token_expires_at = expires_at
        existing.scope = tokens.scope
        # REVOKED is an ORDINARY state on this path, not an exotic one. A member who disconnected
        # a device and then ordered another comes back to the same Withings account, and
        # `_revoke_local_connections` revokes EVERY Withings connection a member has when Withings
        # revoke upstream. Leaving the row revoked while writing live tokens onto it is incoherent
        # and silent: `active_withings_connections` filters on ACTIVE, so the new device is
        # invisible to sync AND to `end_program` — the one with a SIM billing every month — while
        # the route answers 201 and the parcel ships. `ensure_sdk_connection` and `base_oauth`
        # both reactivate on their own reuse paths; this was the outlier (Lucas, #13).
        existing.status = ConnectionStatus.ACTIVE
        existing.updated_at = datetime.now(timezone.utc)
        connection = existing
    else:
        connection = UserConnection(
            **UserConnectionCreate(
                user_id=user_id,
                provider=provider,
                provider_user_id=tokens.userid,
                provider_username=None,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                token_expires_at=expires_at,
                scope=tokens.scope,
            ).model_dump()
        )
        db.add(connection)
    try:
        # flush, not commit: the id has to exist for the SDK row's FK, but nothing is durable
        # until both rows are in.
        db.flush()
        account = _upsert_sdk_account(
            db,
            connection_id=connection.id,
            external_id=external_id,
            csrf_token=tokens.csrf_token,
        )
        db.commit()
    except IntegrityError as e:
        # The reuse above handles the ordinary repeat, so what is left here is a genuine race —
        # two provisionings for the same member in flight at once, one of which committed between
        # the SELECT and this flush — or ``external_id`` already belonging to a DIFFERENT
        # connection, which means the identifier has been reused for something it should not have
        # been. Both mean "a real Withings account exists for this request", and on the cellular
        # path an order with it, so neither may be retried.
        db.rollback()
        raise store_error(
            detail="the Withings account was created but could not be stored: it already exists",
            already_exists=True,
        ) from e

    # AFTER the commit, deliberately. It fired before the upsert and the commit, so a failure in
    # either announced a connection that never persisted — and robin-backend would then hold a
    # connection id that resolves to nothing, with a retry able to announce it twice.
    #
    # Only for a connection that is actually NEW. A repeat order reuses one robin-backend was told
    # about the first time, and re-announcing it would present an existing connection as a fresh
    # one to every consumer of that webhook.
    if not reused:
        on_connection_created(
            user_id=user_id,
            provider=provider,
            connection_id=connection.id,
            connected_at=connection.created_at.isoformat(),
        )

    _schedule_subscription_sync(user_id)

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

    account = _store_provisioned_account(
        db,
        user_id=user_id,
        external_id=sdk_user.external_id,
        tokens=tokens,
    )

    log_structured(
        logger,
        "info",
        "Withings SDK account provisioned",
        provider=ProviderName.WITHINGS.value,
        task="provision_sdk_account",
        user_id=str(user_id),
        external_id=sdk_user.external_id,
    )
    return account


def provision_cellular_order(
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
    unit_pref: dict,
    orders: list[DropshipOrder],
    firstname: str | None = None,
    lastname: str | None = None,
    phonenumber: str | None = None,
    recovery_code: str | None = None,
    testmode: bool = False,
    api_base_url: str | None = None,
) -> CellularProvisioning:
    """Ship a cellular device and store the Withings account it was preconfigured against.

    The same shape as ``provision_sdk_account`` — create the account, exchange the code, persist
    both halves — with ``createuserorder`` in place of ``createuser`` and an order attached.

    **The two halves of the result go to different systems**, which is why this returns a pair
    rather than just the account. The connection and its tokens are health-data plumbing and stay
    here. The orders are commerce — an order id, a shipment status, an address — and belong to
    robin-backend's ``WithingsDeviceOrder``, which is also where the device MACs live that
    ``end_program`` later needs. Nothing about an order is stored in this fork.

    ``external_id`` is per MEMBER and stable across their orders — robin-backend sends the bare
    CustomerProfile id. Withings reuse the account they created for a member on that member's
    later orders, so a second order legitimately arrives with an ``external_id`` we already hold
    and ``_store_provisioned_account`` reuses the connection behind it.

    Which ORDER a shipment belongs to is ``customer_ref_id``, carried on each ``DropshipOrder``.
    It is per-order, unique and ours, and it is what Withings name in their order notifications —
    so it, not ``external_id``, is the value that ties a shipment back to one robin-backend row.
    """
    kwargs = {"api_base_url": api_base_url} if api_base_url else {}

    user_order = create_user_order(
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
        unit_pref=unit_pref,
        orders=orders,
        firstname=firstname,
        lastname=lastname,
        phonenumber=phonenumber,
        recovery_code=recovery_code,
        testmode=testmode,
        **kwargs,
    )

    # Ten minutes, per Withings' own documentation — shorter than a consumer OAuth code. Exchanged
    # immediately and in the same call for that reason: a queued code is a shipped device whose
    # account we can never read.
    tokens: SdkTokens = exchange_sdk_code(
        client_id=client_id,
        client_secret=client_secret,
        code=user_order.code,
        redirect_uri=redirect_uri,
        **kwargs,
    )

    account = _store_provisioned_account(
        db,
        user_id=user_id,
        external_id=user_order.external_id,
        tokens=tokens,
        store_error=WithingsDropshipmentError,
    )

    log_structured(
        logger,
        "info",
        "Withings cellular order provisioned",
        provider=ProviderName.WITHINGS.value,
        task="provision_cellular_order",
        user_id=str(user_id),
        external_id=user_order.external_id,
        order_count=len(user_order.orders),
        testmode=testmode,
    )
    return CellularProvisioning(account=account, orders=user_order.orders)
