"""Withings Mobile SDK provisioning endpoints.

Deliberately NOT tagged "External: Mobile SDK". That tag already means Open Wearables' own
mobile SDK — the one that ingests data from a partner's app — and conflating it with
Withings' device SDK would make the API reference actively misleading.

Everything here is authenticated with the org API key: it provisions a real Withings account
against our partner credentials, so it must never be reachable by an end user.
"""

from logging import getLogger
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.database import DbSession
from app.schemas.enums import ProviderName
from app.services.api_key_service import ApiKeyDep
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
    email: str
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
