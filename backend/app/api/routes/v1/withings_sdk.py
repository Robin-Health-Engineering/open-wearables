"""Withings Mobile SDK provisioning endpoints.

Deliberately NOT tagged "External: Mobile SDK". That tag already means Open Wearables' own
mobile SDK — the one that ingests data from a partner's app — and conflating it with
Withings' device SDK would make the API reference actively misleading.

Everything here is behind ``ApiKeyDep``, which is this codebase's house standard (thirteen route
files use it, ``connections``, ``users``, ``events`` and ``timeseries`` among them) and which
means the org API key **or any authenticated developer JWT** — not the org key alone. What it
rules out is an end user reaching these routes, which is the point: they act against our partner
credentials and provision real Withings accounts.

Worth stating plainly rather than leaving as an implication, because ``GET .../sdk/session`` is
the first route in this codebase to vend a raw provider ``access_token``. That is a bearer
credential for a THIRD PARTY, usable outside Open Wearables entirely, against an account that may
hold more than we ever sync. On a data-read route "an authenticated developer counts as
authorised" is unremarkable; here it means anyone who can authenticate to this deployment can
obtain a live Withings token for any member. That is the boundary as built — and if it is ever
narrowed, it should be narrowed on purpose and not by someone reading a docstring that already
claimed it was.
"""

from logging import getLogger
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.database import DbSession
from app.models.user_connection import UserConnection
from app.models.withings_sdk_account import WithingsSdkAccount
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.services.api_key_service import ApiKeyDep
from app.services.providers.api_client import _get_valid_token
from app.services.providers.factory import ProviderFactory
from app.services.providers.withings.sdk_provisioning import provision_sdk_account
from app.services.providers.withings.sdk_users import WithingsSdkUserError

logger = getLogger(__name__)

router = APIRouter()


class SdkAccountRequest(BaseModel):
    """The profile Withings requires to open an account on the member's behalf.

    Every field here is mandatory at Withings' end. `CustomerProfile` already carries
    birthDate, gender, height and weight, which is why this shape is satisfiable today.
    """

    user_id: UUID = Field(description="Open Wearables user to attach the connection to")
    external_id: str = Field(max_length=64, description="Our own id for this member; the join key")
    # Validated here rather than left to Withings, for the same reason shortname and the enum
    # ranges are: Withings answers bad input with an opaque non-zero status, which surfaces to
    # the caller as a 502 they can do nothing with. A 422 naming the field is the useful answer.
    email: EmailStr
    shortname: str = Field(
        min_length=3,
        max_length=3,
        description="Exactly 3 alphanumerics — Withings renders this ON the device screen",
    )
    birthdate: int = Field(description="Unix timestamp")
    gender: int = Field(ge=0, le=1, description="0 male, 1 female (Withings' vocabulary)")
    weight_kg: float = Field(gt=0)
    height_m: float = Field(gt=0)
    preflang: str = Field(examples=["it_IT"])
    timezone: str = Field(examples=["Europe/Rome"])
    mailingpref: int = Field(ge=0, le=1, description="0 refused, 1 accepted")


class SdkAccountResponse(BaseModel):
    """What the caller needs to open the hosted WebViews.

    The access token is deliberately absent: it expires in three hours, so it is fetched
    separately when a WebView is about to open rather than handed out here to go stale.
    """

    external_id: str
    csrf_token: str


@router.post(
    "/withings/sdk/accounts",
    summary="Provision a Withings SDK account",
    status_code=status.HTTP_201_CREATED,
    tags=["External: Providers"],
)
def create_withings_sdk_account(
    payload: SdkAccountRequest,
    db: DbSession,
    _caller: ApiKeyDep,
) -> SdkAccountResponse:
    """Create a Withings account for one member and store its tokens.

    ⚠️ This OVERWRITES any existing Withings connection for the member. `user_connection` has
    a unique (user_id, provider) index, so a personally-linked account and a provisioned one
    cannot coexist; provisioning wins. The caller is responsible for warning the member first
    — the previous account keeps its history but stops syncing.
    """
    if not settings.withings_client_id or not settings.withings_client_secret:
        # A 503 rather than a 500: the deployment is not configured, which is an operator
        # condition, not a bug in the request.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withings credentials are not configured on this deployment",
        )

    try:
        account = provision_sdk_account(
            db,
            user_id=payload.user_id,
            client_id=settings.withings_client_id,
            client_secret=settings.withings_client_secret.get_secret_value(),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.WITHINGS),
            external_id=payload.external_id,
            email=payload.email,
            shortname=payload.shortname,
            birthdate=payload.birthdate,
            gender=payload.gender,
            weight_kg=payload.weight_kg,
            height_m=payload.height_m,
            preflang=payload.preflang,
            timezone_name=payload.timezone,
            mailingpref=payload.mailingpref,
        )
    except ValueError as e:
        # Local validation (shortname shape, enum ranges) — the caller can fix these.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except WithingsSdkUserError as e:
        # Never echo the upstream body: it answers a signed request and may repeat our
        # parameters. The Withings status is enough to diagnose from the logs.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Withings declined the account creation (status={e.withings_status})",
        ) from e

    # csrf_token is written by provisioning and cannot be null here, but the column is
    # nullable, so assert the invariant rather than hand back a None the client cannot use.
    if not account.csrf_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Withings returned no csrf_token; the account cannot open a WebView",
        )

    return SdkAccountResponse(external_id=account.external_id, csrf_token=account.csrf_token)


class SdkSessionResponse(BaseModel):
    """A live pair for opening a hosted Withings WebView.

    Both are needed and neither is optional: the access token goes on as a secure cookie for
    ``.withings.com``, the csrf_token as a URL parameter. One without the other does not open.
    """

    access_token: str
    csrf_token: str


@router.get(
    "/withings/sdk/session",
    summary="Get a live token pair for the Withings SDK WebViews",
    tags=["External: Providers"],
)
def get_withings_sdk_session(
    user_id: UUID,
    db: DbSession,
    _caller: ApiKeyDep,
) -> SdkSessionResponse:
    """Return a currently-valid access token and csrf_token for one member.

    Fetched per WebView rather than handed out at provisioning: an access token lasts three
    hours, so a value returned at setup time is usually dead by the time a member opens
    device settings.

    ORDER IS LOAD-BEARING. The token is resolved first, which refreshes it if it is within
    five minutes of expiry, and that refresh ROTATES csrf_token. Reading the SDK account
    before refreshing would hand back the pre-rotation value — valid-looking, and rejected by
    Withings. This also reuses the one refresher (`_get_valid_token`, Redis-locked per
    user/provider) rather than adding a second one against a rotating refresh token.
    """
    provider = ProviderName.WITHINGS.value
    strategy = ProviderFactory().get_provider(provider)
    if not strategy.oauth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withings OAuth is not available on this deployment",
        )

    # Refresh-if-needed FIRST — see the docstring. Raises 401 if the member is not connected.
    access_token = _get_valid_token(db, user_id, provider, UserConnectionRepository(), strategy.oauth)

    account = (
        db.query(WithingsSdkAccount)
        .join(UserConnection, WithingsSdkAccount.user_connection_id == UserConnection.id)
        .filter(UserConnection.user_id == user_id, UserConnection.provider == provider)
        .one_or_none()
    )
    if account is None:
        # Connected, but via phase-1 consumer OAuth rather than SDK provisioning. There is no
        # csrf_token because no SDK account was ever created — a distinct condition from "not
        # connected", and worth its own message.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This member has no Withings SDK account; the WebViews need one",
        )
    if not account.csrf_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Withings SDK account has no csrf_token; re-provision it",
        )

    return SdkSessionResponse(access_token=access_token, csrf_token=account.csrf_token)
