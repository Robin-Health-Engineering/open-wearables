"""What ``Order v2 - Getdetail`` says about an order we placed.

Transcribed from ``dropshipment_get_order_status_object`` in Withings' OpenAPI document, which is
the only enumeration of these fields that exists — the dropship-cellular guide pages describe the
notification and not this response.

Lenient in the same way ``DropshipOrderResult`` is, and for the same reason: this is read AFTER a
device has shipped, so a field Withings add or a type they widen must not turn a successful
lookup into an exception. Everything is optional and unknown keys are kept.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OrderDetailDevice(BaseModel):
    """One physical unit inside a product line.

    ``mac_address`` is the field this module exists for: it is what
    ``Devicev2-endpartnerprogram`` is addressed by, and therefore the only way to stop a cellular
    plan billing after a member leaves. Withings populate it **only once the order has shipped**.
    """

    model_config = ConfigDict(extra="allow")

    serial_number: str | None = None
    mac_address: str | None = None
    hash_deviceid: str | None = None
    model: str | None = None
    model_id: int | None = None


class OrderDetailProduct(BaseModel):
    """One line of the order, and the units that satisfied it.

    Withings report the MACs **twice** — flat in ``mac_addresses`` and per unit in ``devices`` —
    and this keeps both rather than picking one. The flat list is the documented field; the nested
    one carries the serial and model alongside. Neither is guaranteed present on a given response,
    so ``macs`` below unions them.
    """

    model_config = ConfigDict(extra="allow")

    ean: str | None = None
    partner_ref: str | None = None
    quantity: int | None = None
    devices: list[OrderDetailDevice] = []
    mac_addresses: list[str] = []
    hash_deviceids: list[str] = []
    serial_numbers: list[str] = []


class OrderDetail(BaseModel):
    """One order, as Withings currently hold it.

    The same two-vocabulary split the notification uses: ``status`` is the order's own state while
    Withings have it, ``parcel_status`` the carrier's once it has left. Kept verbatim — mapping
    them onto anyone's timeline is the caller's business, and in this system that caller is
    robin-backend.
    """

    model_config = ConfigDict(extra="allow")

    order_id: str | None = None
    customer_ref_id: str | None = None
    status: str | None = None
    parcel_status: str | None = None
    carrier: str | None = None
    carrier_service: str | None = None
    tracking_number: str | None = None
    ship_date: str | None = None
    is_replacement: bool | None = None
    original_customer_ref: str | None = None
    products: list[OrderDetailProduct] = []

    @property
    def macs(self) -> list[str]:
        """Every MAC on this order, de-duplicated, in the order Withings listed them.

        Unions the two places Withings report them. Empty is the NORMAL answer before the order
        ships, and it is not distinguishable here from "shipped but Withings have not populated
        it" — a caller that needs the difference must look at ``status``.

        De-duplicated because the flat and nested lists describe the same hardware, and a MAC
        passed twice to End of Program is a second call that can only fail.
        """
        seen: dict[str, None] = {}
        for product in self.products:
            for mac in product.mac_addresses:
                if mac:
                    seen.setdefault(mac, None)
            for device in product.devices:
                if device.mac_address:
                    seen.setdefault(device.mac_address, None)
        return list(seen)
