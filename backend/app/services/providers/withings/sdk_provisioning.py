"""Provision a Withings SDK account for a member and persist the result.

Ties together the three steps that must succeed as one: ``createuser``, the code exchange,
and storing what came back. Split across callers they can half-succeed, and a half-succeeded
provisioning is the bad case — a connection with no ``csrf_token`` looks healthy and then
cannot open a WebView.

A member may hold SEVERAL Withings connections at once, and provisioning adds one rather than
replacing what is there. Two facts force it: Withings creates an account on every provisioning
path they offer, and a cellular device cannot be activated onto an account the partner did not
create. So a member who has linked their own Withings account and is then shipped a device
holds two, and another for every later order.

This used to overwrite instead, because the unique index was ``(user_id, provider)`` and there
was nowhere to put a second row — which meant shipping someone a blood-pressure monitor
silently stopped their own scale and watch from syncing. The index now includes
``provider_user_id``; provisioning the SAME Withings account twice still fails there, which is
the bug worth keeping a constraint for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from app.database import DbSession
from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
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
    # CREATE, never replace. A member can hold several Withings accounts — their own, linked
    # through consumer OAuth, plus one for every cellular order, because Withings creates an
    # account on every provisioning path and a device cannot be added to an account that
    # already exists.
    #
    # This branch used to find any existing Withings connection and overwrite it, which meant
    # shipping someone a blood-pressure monitor silently stopped their own scale and watch from
    # syncing. The unique index now keys on (user_id, provider, provider_user_id), so the two
    # coexist; provisioning the SAME Withings account twice still fails there, which is right.
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
    connection = UserConnection(
        **UserConnectionCreate(
            user_id=user_id,
            provider=provider,
            provider_user_id=tokens.userid,
            provider_username=None,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in),
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
        # Either unique index can fire: (user_id, provider, provider_user_id) if Withings handed
        # back an account this member already holds, or external_id if this exact provisioning
        # already ran. Both mean "a real Withings account exists for this request" — and on the
        # cellular path, an order with it.
        db.rollback()
        raise store_error(
            detail="the Withings account was created but could not be stored: it already exists",
            already_exists=True,
        ) from e

    # AFTER the commit, deliberately. It fired before the upsert and the commit, so a failure in
    # either announced a connection that never persisted — and robin-backend would then hold a
    # connection id that resolves to nothing, with a retry able to announce it twice.
    on_connection_created(
        user_id=user_id,
        provider=provider,
        connection_id=connection.id,
        connected_at=connection.created_at.isoformat(),
    )

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

    ``external_id`` must be unique per ACCOUNT, not per member: ``withings_sdk_account`` enforces
    that, and a member gets one account per cellular order. robin-backend sends
    ``{customerProfileId}#{orderRef}`` for exactly this reason — passing a bare profile id here
    for a member's second order fails on that constraint, after Withings has already created the
    account and placed the order.
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
