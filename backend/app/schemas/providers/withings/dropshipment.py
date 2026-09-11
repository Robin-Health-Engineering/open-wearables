"""The ``order`` block ``createuserorder`` takes, and what it gives back.

Modelled rather than passed through as a dict for one reason: every field here is validated by
Withings and rejected as an opaque non-zero status, and half of them are a physical address that
a parcel is about to be sent to. A typo in ``country`` costs a shipment, not a 400.

Reference: developer-guide/v3/integration-guide/dropship-cellular/logistics-api/create-user-order
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DropshipAddress(BaseModel):
    """Where the parcel goes.

    ``state`` and ``country`` are ISO2 and ``telephone`` is E.164 — Withings' constraints, not
    ours. They are not enforced here beyond length: this data comes from a member filling in a
    form, and rejecting an order locally on a format guess would be worse than letting Withings
    say what it will not accept.
    """

    name: str = Field(max_length=255)
    email: str = Field(max_length=255)
    address1: str = Field(max_length=255)
    address2: str | None = Field(default=None, max_length=255)
    city: str = Field(max_length=128)
    zip: str = Field(max_length=32)
    state: str | None = Field(default=None, max_length=2)
    country: str = Field(min_length=2, max_length=2)
    company_name: str | None = Field(default=None, max_length=255)
    telephone: str | None = Field(default=None, max_length=32)


class DropshipProduct(BaseModel):
    """One line of the order.

    Withings identifies a product by EITHER its ``ean`` or a ``partner_ref`` they have agreed
    with us, and requires exactly one to be present. Checked here rather than left to the API
    because "neither" and "both" come back as the same opaque status.
    """

    quantity: int = Field(ge=1, le=50)
    ean: str | None = Field(default=None, max_length=64)
    partner_ref: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> "DropshipProduct":
        if bool(self.ean) == bool(self.partner_ref):
            raise ValueError("a dropshipment product needs exactly one of ean or partner_ref")
        return self


class DropshipOrder(BaseModel):
    """One order: an address and the products going to it.

    ``customer_ref_id`` is OURS and must be unique per order. It is the join back to the
    robin-backend row that owns this order's status, and it is also the suffix of the
    ``external_id`` we send for the account — so the same value ties the Withings account, the
    Withings order and our own record together.
    """

    customer_ref_id: str = Field(max_length=64)
    address: DropshipAddress
    products: list[DropshipProduct] = Field(min_length=1)
    # Withings validates the address and rejects one it cannot deliver to. Bypassing that is a
    # deliberate act for a member whose address is real and unusual, never a default.
    force_address: bool = False


class DropshipOrderResult(BaseModel):
    """One order as Withings acknowledged it.

    Loose on purpose: ``status`` is Withings' own vocabulary and they add to it, and the echoed
    address is theirs rather than ours. Parsing this strictly would fail a shipment that has
    already been placed, which is the one outcome worth avoiding here.
    """

    model_config = ConfigDict(extra="allow")

    orderid: str | None = None
    status: str | None = None
    address: dict | None = None


class DropshipUserOrder(BaseModel):
    """What ``createuserorder`` returns: an account, and the orders placed for it.

    The two halves go to different places. ``code``/``external_id`` become a ``user_connection``
    here; the orders belong to robin-backend, which owns shipment state — so this model exists to
    carry them back out of the provisioning call rather than to store them.
    """

    code: str
    external_id: str
    orders: list[DropshipOrderResult] = Field(default_factory=list)
