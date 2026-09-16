"""Withings Mobile SDK endpoints: account provisioning, WebView sessions, and devices.

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

from datetime import datetime
from logging import getLogger
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.config import settings
from app.database import DbSession
from app.models.user_connection import UserConnection
from app.models.withings_device import WithingsDevice
from app.models.withings_sdk_account import WithingsSdkAccount
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.schemas.providers.withings.dropshipment import DropshipOrder, DropshipOrderResult
from app.schemas.providers.withings.order_detail import OrderDetail
from app.services.api_key_service import ApiKeyDep
from app.services.providers.api_client import _get_valid_token
from app.services.providers.factory import ProviderFactory
from app.services.providers.withings.dropshipment import WithingsDropshipmentError
from app.services.providers.withings.order_detail import WithingsOrderDetailError, get_order_detail
from app.services.providers.withings.sdk_devices import (
    WithingsDeviceError,
    list_devices,
    mark_dissociated,
    record_installed_device,
    sync_devices_from_withings,
)
from app.services.providers.withings.sdk_provisioning import (
    provision_cellular_order,
    provision_sdk_account,
)
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
        if e.already_exists:
            # A duplicate account is the caller's state, not a server fault — this route used to
            # answer 400 here via an HTTPException escaping the repository's @handle_exceptions,
            # and that shape was right even though its mechanism was not. Preserved deliberately
            # now that the store writes both rows in one transaction and no longer goes through
            # the repository.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
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


class CellularOrderRequest(SdkAccountRequest):
    """What robin-backend sends to ship a cellular device.

    Deliberately the SDK account request plus the two fields ``createuserorder`` adds, so the
    caller builds ONE profile shape for both provisioning paths — the app's
    ``buildSdkAccountRequest`` is the single producer of both, and a second field list would
    drift from it silently.

    Three things differ from the parent and each is load-bearing:

    ``external_id`` widens to 128. Both routes now send a bare profile id — Withings reuse a
    member's provisioned account on their later orders — so the extra room is headroom rather
    than a live requirement. It is kept because narrowing a column needs a migration and buys
    nothing, and because the value is robin-backend's to choose.

    ``unit_pref`` and ``orders`` are new. Both are required by ``createuserorder`` and neither
    has a sensible default: the first decides what the device screen displays, the second is the
    parcel.
    """

    external_id: str = Field(
        max_length=128, description="The CustomerProfile id — one per MEMBER, stable across their orders"
    )
    unit_pref: dict[str, int] = Field(
        min_length=1, description="Withings' unit vocabulary, e.g. {'weight': 1, 'height': 6}"
    )
    # ``min_length=1`` is the whole reason this is validated here. ``createuserorder`` with an
    # empty order list creates the account, ships nothing, and answers 0 — a success that
    # delivered no device, which is the one outcome this integration must never report.
    orders: list[DropshipOrder] = Field(min_length=1)
    firstname: str | None = Field(default=None, max_length=255)
    lastname: str | None = Field(default=None, max_length=255)
    phonenumber: str | None = Field(default=None, max_length=32)
    recovery_code: str | None = Field(default=None, max_length=64)
    # Places a real order against Withings without shipping hardware. The first exercise of this
    # route runs with it on, and it is the reason ``create_user_order`` already carries the flag.
    testmode: bool = False


class CellularOrderResponse(BaseModel):
    """The two halves of a cellular provisioning, kept apart.

    ``external_id``/``csrf_token`` are the account, which lives here. ``orders`` are commerce —
    an order id and a shipment status — and belong to robin-backend's ``WithingsDeviceOrder``,
    which is also where the device MACs that ``end_program`` needs will land. Nothing about an
    order is stored in this fork, so this response is the only place the caller can get it.
    """

    external_id: str
    csrf_token: str
    orders: list[DropshipOrderResult]


def _fill_missing_order_ids(
    orders: list[DropshipOrderResult], refs: list[str], *, client_id: str, client_secret: str
) -> None:
    """Resolve an order id Withings acknowledged the order WITHOUT, by asking getdetail.

    Withings answered a live `createuserorder` with status 0 and an order entry carrying no
    `orderid` (2026-09-16, order `D0820568`). The order was real and a device shipped against it;
    `getdetail` on the same `customer_ref_id` returned the id seconds later. The documented
    response example shows `orderid` present, so this is an undocumented shape rather than a
    parsing mistake — we read the right key and it was not there.

    **What made that expensive is what the CALLER does with an id-less order.** robin-backend
    treats a missing id as "no order was placed" and records a fulfilment failure, which is the
    correct reading of the documented contract and the exact opposite of the truth: the parcel was
    already on its way, with nothing in our systems naming it. The device MAC — the only thing
    `Devicev2-endpartnerprogram` can be addressed by — is fetched against that order, so an
    untracked order is a SIM that cannot be terminated when the member leaves.

    So the contract this route offers is tightened rather than the caller being taught the
    exception: an order in the response carries its id whenever Withings know one.

    Two things it deliberately does NOT do:

    * **It never raises, and the catch is deliberately broad.** The order exists by the time this
      runs and the `csrf_token` beside it is single-use, so ANY exception escaping here loses the
      member their account to save a field. `WithingsOrderDetailError` is not the only way out:
      `get_order_detail` calls `acquire_request_slot()` and `sign_payload()` OUTSIDE its own try,
      so an exhausted Withings budget escapes as `HTTPException` 429, an unreachable Redis as 503,
      and a signing failure as `WithingsSignatureError` — none of them subclasses of this module's
      error. The pre-flight above documents that same mechanism and has already spent a slot on
      this request, which makes the 429 the LIKELY escape rather than a theoretical one (ross,
      #15). So this catches `Exception`: there is no failure of an optional enrichment that is
      worth more than the account it would destroy.
    * **It never guesses which order is which.** `createuserorder` echoes no `customer_ref_id` on
      its order entries, so with several orders in flight the only link is position, and a wrong
      pairing writes one parcel's id onto another — invisible until someone terminates the wrong
      device. It fills only the unambiguous case (one unidentified order, one unaccounted-for ref)
      and logs the rest for a human.

    Note the field names differ by endpoint and that is not a typo here: `createuserorder` answers
    `orderid`, `getdetail` answers `order_id`.

    Credentials are passed in rather than read from `settings` here: the handler has already
    checked they are present (and answered 503 if not), and re-reading them in a module-level
    helper widens them back to `str | None` for no gain.
    """
    missing = [order for order in orders if not order.orderid]
    if not missing:
        return

    try:
        known = get_order_detail(client_id=client_id, client_secret=client_secret, customer_ref_ids=refs)
    except Exception as e:
        # Broad on purpose — see the docstring. `withings_status` exists only on this module's own
        # error, so it is read defensively rather than assumed; the type name is logged because a
        # 429 here and a Withings 503 are different operational problems with the same outcome.
        logger.error(
            "Withings acknowledged an order with no orderid and getdetail could not resolve it — "
            "the order IS placed; recover it by customer_ref_id",
            extra={
                "error_type": type(e).__name__,
                "withings_status": getattr(e, "withings_status", None),
                "reason": str(e),
                "customer_ref_ids": refs,
            },
        )
        return

    already = {order.orderid for order in orders if order.orderid}
    unaccounted = [detail for detail in known if detail.order_id and detail.order_id not in already]

    if len(missing) == 1 and len(unaccounted) == 1:
        missing[0].orderid = unaccounted[0].order_id
        logger.warning(
            "Withings acknowledged an order with no orderid; resolved it from getdetail",
            extra={"customer_ref_id": unaccounted[0].customer_ref_id, "order_id": unaccounted[0].order_id},
        )
        return

    logger.error(
        "Withings acknowledged an order with no orderid and it cannot be matched unambiguously — "
        "the order IS placed; recover it by customer_ref_id",
        extra={"customer_ref_ids": refs, "missing": len(missing), "unaccounted": len(unaccounted)},
    )


@router.post(
    "/withings/cellular/orders",
    summary="Create a Withings account and ship a cellular device to the member",
    status_code=status.HTTP_201_CREATED,
    tags=["External: Providers"],
)
def create_withings_cellular_order(
    payload: CellularOrderRequest,
    db: DbSession,
    _caller: ApiKeyDep,
) -> CellularOrderResponse:
    """Provision a cellular device: one signed Withings call, two halves of a result.

    Unlike ``POST /withings/sdk/accounts`` this does NOT overwrite the member's existing Withings
    connection. A cellular device cannot be activated onto an account we did not create, so a
    member holds the account we made plus, possibly, their own — which is what the three-column
    ``ix_user_connection_user_provider`` was widened to allow. A member's SECOND order reuses the
    first one's account, per Withings, so it reuses that connection rather than adding a third.

    Called only from robin-backend's ``fulfillWithingsDeviceOrder``, after Stripe confirms the
    payment. That ordering is the caller's to keep: this route ships a parcel, and nothing here
    knows whether it was paid for.

    **WHO MAY CALL THIS, stated here rather than inherited.** ``ApiKeyDep`` admits the org API key
    OR any authenticated developer JWT, with no per-member, per-org or per-scope narrowing, and
    ``payload.user_id`` is never checked against the caller. The module docstring already concedes
    that shape — but it is reasoning about READING data and vending tokens. This route spends money
    and ships a physical parcel to an address the CALLER chooses, which is a different blast radius
    under the same sentence, so it is restated: ordering authority is trusted to any principal that
    can authenticate to this deployment. If that is ever too wide, the credential is what to narrow
    — not this docstring (Lucas, #12).

    **RETRIES ARE NOT SAFE, and this route cannot make them safe.** ``createuserorder`` is not
    idempotent at Withings and we hold no idempotency key they honour, so a second call places a
    SECOND ORDER before anything here can object. The pre-flight below turns the ordinary
    sequential retry into a 409 before any Withings call, which is the case that actually occurs:
    robin-backend retries ``fulfillWithingsDeviceOrder`` after a timeout, with the same
    ``customer_ref_id``.

    **The pre-flight asks WITHINGS, not our own tables, and that change is load-bearing.** It used
    to check whether a ``withings_sdk_account`` already held this ``external_id``. That worked only
    while ``external_id`` was per-order; now it is per member and stable, so "an account exists"
    is true from a member's FIRST order onward and would refuse every legitimate second one.

    ``orderv2-getdetail`` answers the question the old check was approximating, and answers it
    better: ``customer_ref_id`` is per-order, unique and ours, and Withings are the authority on
    whether an order carrying it exists. That also closes the gap the old check conceded — a crash
    between Withings accepting and our commit leaves no local trace, but Withings still hold the
    order and will say so.

    One gap remains, un-papered-over: it is a TOCTOU narrowing, not a fix. Two SIMULTANEOUS
    fulfilments can both see "no order" and both ship. Closing that needs an idempotency key
    Withings honour, which they do not offer.

    A 409 therefore means "Withings already hold an order under this customer_ref_id". The caller
    must treat it as terminal and reconcile, NOT retry — retrying can only ship again.
    """
    if not settings.withings_client_id or not settings.withings_client_secret:
        # An operator condition, not a bad request — the same 503 the SDK route answers.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withings credentials are not configured on this deployment",
        )

    # PRE-FLIGHT, before any Withings call — see the docstring. One signed round trip against the
    # authority, and it is the difference between a retry costing a round trip and a retry costing
    # a second parcel.
    #
    # It is NOT best-effort. "We could not find out whether this order exists" and "this order
    # does not exist" are different facts, and treating the first as the second is precisely how a
    # retry ships twice — so every failure REFUSES, and only an empty answer falls through.
    #
    # The refusal is not always this 502, and the difference is worth knowing when reading logs:
    # `acquire_request_slot()` sits outside `get_order_detail`'s try, so an exhausted shared
    # Withings budget escapes as 429 and an unreachable Redis as 503, neither reaching the handler
    # below. Three answers, one property — nothing degrades to "no order, ship it". The 429 is not
    # hypothetical now that the pre-flight spends a slot on every fulfilment (Lucas, #13).
    #
    # 502 rather than 409 because getdetail changes nothing at Withings: this one IS safe to
    # retry, which is the opposite of a 502 from the order call itself.
    refs = [order.customer_ref_id for order in payload.orders]
    try:
        placed = get_order_detail(
            client_id=settings.withings_client_id,
            client_secret=settings.withings_client_secret.get_secret_value(),
            customer_ref_ids=refs,
        )
    except WithingsOrderDetailError as e:
        # `withings_status` is None for everything except a non-zero Withings envelope — the HTTP
        # and transport branches construct the error with `detail=` only — so the message is
        # carried too rather than leaving an uninformative line at the place someone looks first.
        # It is safe to echo for the reason audited on the branch below: every `detail=` handed to
        # a WithingsOrderDetailError is a fixed string this codebase writes, never a response body.
        logger.error(
            "Withings cellular order: could not check whether the order was already placed",
            extra={"withings_status": e.withings_status, "reason": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not verify with Withings whether this order was already placed",
        ) from e
    if placed:
        logger.error(
            "Withings cellular order: an order already exists for this customer_ref_id",
            extra={"order_count": len(placed)},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Withings already hold an order for this customer_ref_id; it was already placed",
        )

    try:
        result = provision_cellular_order(
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
            unit_pref=payload.unit_pref,
            orders=payload.orders,
            firstname=payload.firstname,
            lastname=payload.lastname,
            phonenumber=payload.phonenumber,
            recovery_code=payload.recovery_code,
            testmode=payload.testmode,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except WithingsSdkUserError as e:
        # The CODE EXCHANGE, which runs AFTER createuserorder has already placed the order. Without
        # this the exception escapes both handlers and the caller gets a bare 500 with a device on
        # its way — the failure mode the SDK route has always handled and this one inherited none
        # of, because it catches the dropshipment error and the exchange raises a different type.
        #
        # No disclosure: FastAPI's generic handler answers a plain "Internal Server Error" and the
        # message reaches the structured log only. What the 500 cost was the stranded order and an
        # uninterpretable status, not a leak (Lucas verified this correction, #12).
        logger.error(
            "Withings cellular order: the code exchange failed AFTER the order was placed",
            extra={"external_id": payload.external_id, "withings_status": e.withings_status},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Withings placed the order but the code exchange failed (status={e.withings_status})",
        ) from e
    except WithingsDropshipmentError as e:
        if e.already_exists:
            # The store lost the UNIQUE race: two provisionings for this member were in flight and
            # the other committed first, or `external_id` belongs to a different connection. The
            # ordinary repeat no longer reaches here at all — `_store_provisioned_account` reuses
            # the connection — so what is left is a genuine conflict, and it means the same thing
            # as the pre-flight 409: an order exists, do not retry.
            #
            # There is deliberately no `except IntegrityError` beside this. There was one until
            # Lucas measured the real path on #12 and found it could not fire: every write in
            # `_store_provisioned_account` is inside a try that converts IntegrityError to
            # `store_error`, so nothing IntegrityError-shaped ever reaches this route. A handler
            # for an exception that cannot arrive reads as protection and provides none — and the
            # test I had written for it forced the exception in artificially, so it passed while
            # the real concurrent race was answering 502, the one status a generic retry policy
            # WOULD have retried.
            logger.error(
                "Withings cellular order: the account already exists — a concurrent provisioning won",
                extra={"external_id": payload.external_id},
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account already exists for this external_id; the order was already placed",
            ) from e
        # NEVER the upstream body. It answers a signed payload that, on this route, carries the
        # member's home address as well as their email, birth date and weight.
        #
        # `withings_status` is set only on the non-zero-envelope branch (dropshipment.py:199); the
        # HTTPStatusError branch and the four detail-only raises leave it None, and
        # "status=None" reads as though Withings answered without a status rather than as a
        # transport or contract failure. Fall back to the exception's own message (Lucas, #12).
        #
        # Safe to echo, and audited rather than assumed: every `detail=` handed to
        # WithingsDropshipmentError is a fixed string this codebase writes — the six raises in
        # dropshipment.py (lines 169, 180, 185, 207, 246, 254) plus the store failure in
        # sdk_provisioning.py. None interpolates a response body or a payload field. A future
        # `detail=f"...{response.text}"` would turn this line into the leak the 277 branch exists
        # to prevent, so keep that invariant when adding a raise.
        detail = (
            f"Withings declined the cellular order (status={e.withings_status})"
            if e.withings_status is not None
            else f"Withings cellular order failed: {e}"
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from e

    if not result.account.csrf_token:
        # Same invariant as the SDK route, and worse to discover late here: the order is already
        # placed, so a null hands the caller an account it can never open a WebView against.
        #
        # The orders are logged FIRST because raising discards them, and this fork stores nothing
        # about an order by design — so the order ids exist in this value and nowhere else. Losing
        # them leaves a shipment that nothing can track and that `end_program` can never terminate.
        logger.error(
            "Withings cellular order placed but no csrf_token came back — order ids logged so the "
            "shipment stays recoverable",
            extra={
                "external_id": result.account.external_id,
                "orders": [o.model_dump(mode="json") for o in result.orders],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Withings returned no csrf_token; the account cannot open a WebView",
        )

    # After the csrf_token guard, not before it: that branch already logs the order ids and
    # raises, so a resolve there would spend a Withings request to enrich a value about to be
    # discarded.
    _fill_missing_order_ids(
        result.orders,
        refs,
        client_id=settings.withings_client_id,
        client_secret=settings.withings_client_secret.get_secret_value(),
    )

    return CellularOrderResponse(
        external_id=result.account.external_id,
        csrf_token=result.account.csrf_token,
        orders=result.orders,
    )


class CellularOrderDetailResponse(BaseModel):
    """What Withings currently say about orders we placed, plus the MACs once they exist.

    ``macs`` is the reason this route exists and is lifted to the top level rather than left
    inside ``orders[].products[].devices[]``: the caller needs one flat list to hand to
    ``owWithingsDisconnect``, and burying it three levels down invites each caller to walk the
    structure differently. ``orders`` is the raw answer beside it, because shipment state is
    robin-backend's and this fork must not become the thing that decides which fields matter.

    An EMPTY ``macs`` is the normal answer before a device ships — Withings populate MAC addresses
    only once the parcel has left — and it is NOT distinguishable here from "shipped but not yet
    populated". A caller that needs the difference has ``orders[].status``.
    """

    macs: list[str]
    orders: list[OrderDetail]


@router.get(
    "/withings/cellular/orders/{customer_ref_id}",
    summary="What Withings say about a cellular order, including its device MAC addresses",
    tags=["External: Providers"],
)
def get_withings_cellular_order(
    customer_ref_id: str,
    _caller: ApiKeyDep,
) -> CellularOrderDetailResponse:
    """Read one order back from Withings by OUR reference.

    **This is the MAC source, and the MAC is the field with money attached.**
    ``Devicev2-endpartnerprogram`` ends a member's cellular plan and is addressed by MAC address.
    ``end_program`` has been written and unreachable since #7 because nothing produced one: the
    ``createuserorder`` response carries no MAC, this fork stores none, and Withings populate
    ``products[].mac_addresses`` only once the order has shipped. So the flow is
    robin-backend's order webhook sees SHIPPED, robin-backend calls this, and the MACs land on the
    ``WithingsDeviceOrder`` row that ``owWithingsDisconnect`` already reads them from. Without
    this route that chain has no first link and a member who leaves keeps costing us a SIM.

    **Why it is here rather than in robin-backend**, which owns every other fact about an order:
    the call is signed with the Withings client secret in the application's own name, and that
    secret lives in this codebase and should live in exactly one place.

    Nothing is stored. The answer goes straight back to the caller, which is the same division
    ``POST /withings/cellular/orders`` makes with its ``orders`` field.

    404 means Withings do not know this reference — either it was never placed, or it was placed
    against a different ``client_id`` (staging versus prod). It does **not** mean the order failed.
    """
    if not settings.withings_client_id or not settings.withings_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withings credentials are not configured on this deployment",
        )

    try:
        orders = get_order_detail(
            client_id=settings.withings_client_id,
            client_secret=settings.withings_client_secret.get_secret_value(),
            customer_ref_ids=[customer_ref_id],
        )
    except WithingsOrderDetailError as e:
        # Never the upstream body: the order it echoes carries the member's home address.
        logger.error(
            "Withings cellular order detail failed",
            extra={"withings_status": e.withings_status},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Withings declined the order lookup (status={e.withings_status})"
            if e.withings_status is not None
            else f"Withings order lookup failed: {e}",
        ) from e

    if not orders:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Withings hold no order for this customer_ref_id",
        )

    # De-duplicated across orders as well as within one: a replacement shipment can repeat a MAC,
    # and End of Program called twice for the same device is a second call that can only fail.
    macs: dict[str, None] = {}
    for order in orders:
        for mac in order.macs:
            macs.setdefault(mac, None)

    return CellularOrderDetailResponse(macs=list(macs), orders=orders)


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


class SdkDeviceResponse(BaseModel):
    """One of the member's devices, as the app needs it.

    Display state only — what the device is, when it last synced, how its battery is doing.
    Nothing here identifies the member or authenticates anything. What we SHIPPED them (order,
    status, MAC) is robin-backend's to hold, not ours.
    """

    # ``model_id`` trips Pydantic's protected "model_" namespace. Safe to disable: nothing
    # here shadows BaseModel's own API, it is only a field whose name starts with those six
    # characters. Stated rather than left bare so the next person does not remove it as
    # unexplained — same as SdkDeviceInstallRequest and WithingsDeviceEntry.
    model_config = ConfigDict(protected_namespaces=())

    device_id: str
    model_id: int | None
    model: str | None
    device_type: str | None
    battery: str | None
    last_session_at: datetime | None
    dissociated_at: datetime | None

    @classmethod
    def of(cls, device: WithingsDevice) -> "SdkDeviceResponse":
        return cls(
            device_id=device.device_id,
            model_id=device.model_id,
            model=device.model,
            device_type=device.device_type,
            battery=device.battery,
            last_session_at=device.last_session_at,
            dissociated_at=device.dissociated_at,
        )


class SdkDeviceInstallRequest(BaseModel):
    """What an install-success notification gave the app.

    Only ``user_id`` and ``device_id`` are required; the rest is reported as Withings reported
    it, and a notification that omits a field must not cost us the device record.

    ``advertise_key`` is no longer a field here, and Pydantic's default ``extra="ignore"`` means
    an older app build still sending one has it **silently dropped** rather than rejected. That
    is deliberate — lenient is the right posture toward a client we have not shipped yet — but it
    is worth stating, because "the model no longer accepts it" reads as though the value could
    not be sent, and what actually happens is that it is accepted and discarded.
    """

    model_config = ConfigDict(protected_namespaces=())

    user_id: UUID
    device_id: str = Field(max_length=64)
    model_id: int | None = None
    model: str | None = Field(default=None, max_length=64)


@router.post(
    "/withings/sdk/devices",
    summary="Record a device from the SDK's install-success notification",
    status_code=status.HTTP_201_CREATED,
    tags=["External: Providers"],
)
def record_withings_device(
    payload: SdkDeviceInstallRequest,
    db: DbSession,
    _caller: ApiKeyDep,
) -> SdkDeviceResponse:
    """Store the device the member just finished setting up.

    Pre-registers a device ahead of the Getdevice sweep, which may not list a just-installed one
    yet. Its original justification was ``advertise_key``, the Mobile SDK's background-BLE token;
    that integration is abandoned and the field is gone, so this endpoint now only buys earlier
    visibility. It is a candidate for retirement along with the rest of the SDK surface.

    Idempotent on ``(member, device_id)`` — the app may retry, and a member may re-run setup
    on a device they already own.
    """
    try:
        device = record_installed_device(
            db,
            user_id=payload.user_id,
            device_id=payload.device_id,
            model_id=payload.model_id,
            model=payload.model,
        )
    except WithingsDeviceError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return SdkDeviceResponse.of(device)


@router.get(
    "/withings/sdk/devices",
    summary="List a member's Withings devices",
    tags=["External: Providers"],
)
def list_withings_devices(
    user_id: UUID,
    db: DbSession,
    _caller: ApiKeyDep,
    include_dissociated: bool = False,
) -> list[SdkDeviceResponse]:
    """Return what we hold, without calling Withings.

    A pure read of our own rows, so it is safe to call on every render of the device hub. Use
    the sync endpoint when the answer needs to be current — after the settings WebView closes,
    for instance, where the member may have dissociated something.
    """
    try:
        devices = list_devices(db, user_id=user_id, include_dissociated=include_dissociated)
    except WithingsDeviceError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return [SdkDeviceResponse.of(d) for d in devices]


@router.post(
    "/withings/sdk/devices/sync",
    summary="Reconcile a member's devices against Withings",
    tags=["External: Providers"],
)
def sync_withings_devices(
    user_id: UUID,
    db: DbSession,
    _caller: ApiKeyDep,
) -> list[SdkDeviceResponse]:
    """Fetch ``User v2 - Getdevice`` and reconcile it into our rows.

    The authoritative source, and the only one that survives an app reinstall — which loses
    every notification the app ever received. Also the only way to learn that a member
    dissociated a device on Withings' own side.

    A POST because it writes: it upserts every listed device and marks the ones Withings no
    longer lists as dissociated. It never erases a field the response merely omits.
    """
    strategy = ProviderFactory().get_provider(ProviderName.WITHINGS.value)
    if not strategy.oauth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Withings OAuth is not available on this deployment",
        )
    try:
        devices = sync_devices_from_withings(db, user_id=user_id, oauth=strategy.oauth)
    except WithingsDeviceError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return [SdkDeviceResponse.of(d) for d in devices]


@router.delete(
    "/withings/sdk/devices/{device_id}",
    summary="Record that a device was dissociated",
    tags=["External: Providers"],
)
def dissociate_withings_device(
    device_id: str,
    user_id: UUID,
    db: DbSession,
    _caller: ApiKeyDep,
) -> SdkDeviceResponse | None:
    """Mark a device removed, from the SDK's dissociation-success notification.

    A soft marker, not a delete — see the model. Returns ``null`` when we hold no such device,
    which is not an error: the member may have dissociated one set up before we started
    recording them, or from another phone.
    """
    try:
        device = mark_dissociated(db, user_id=user_id, device_id=device_id)
    except WithingsDeviceError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return SdkDeviceResponse.of(device) if device else None
