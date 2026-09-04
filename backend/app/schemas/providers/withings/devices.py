"""Model the ``User v2 - Getdevice`` payload, one of the two sources of ``advertise_key``."""

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class WithingsDeviceEntry(BaseModel):
    """One entry of ``getdevice``'s ``devices`` list.

    Every field but ``deviceid`` is optional, and that is not defensive padding: this response
    is shaped by what the member owns and by whether our partner scope carries the SDK fields.
    A parse that insisted on the full shape would fail the whole sync because one device did
    not report a battery level.
    """

    # ``model_id`` trips Pydantic's protected "model_" namespace, which is a warning about a
    # name collision with BaseModel's own API rather than a problem with the field.
    model_config = ConfigDict(protected_namespaces=())

    deviceid: str

    # Withings' numeric model. Their responses have carried both spellings across API
    # versions, so accept either rather than silently parse it as absent — a null model_id
    # means the setup WebView cannot be opened straight onto the right device.
    model_id: int | None = Field(default=None, validation_alias=AliasChoices("model_id", "modelid"))
    model: str | None = None
    type: str | None = None

    # THE reason this schema exists. Withings documents two sources for it and requires both
    # to be implemented; this is the second. Absent here is normal and not an error — the
    # install-success notification may already have supplied it, and a Wi-Fi-only device that
    # never fell back to BLE has no use for one.
    advertise_key: str | None = None

    # Unix seconds. What "last synced" on the device hub is built from.
    last_session_date: int | None = None


class WithingsGetdeviceBody(BaseModel):
    """The unwrapped ``body`` of a ``getdevice`` response."""

    devices: list[WithingsDeviceEntry] = []
