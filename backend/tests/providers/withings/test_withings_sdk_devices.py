"""Two writers of one device row, and the rule that keeps them from destroying each other.

A member's devices are reported by ``User v2 - Getdevice`` and, slightly sooner, by an
install-success notification. Getdevice is authoritative and the only source that survives an
app reinstall; the notification merely gets the row in before the next sweep.

The invariant these tests exist for is the one that is easy to break by writing the obvious
code: **a write never erases what it cannot replace.** Getdevice shapes its response by what
each device reports, so an entry omitting ``battery`` means "this device did not say", not
"there is no battery level" — and the stored value must survive it.

That rule was originally about ``advertise_key``, the Mobile SDK's background-BLE token. That
integration is abandoned and the column is gone; the rule outlived it, because the response is
still shaped by what each device reports.

Real session fixture rather than mocks, because every one of these is a claim about the row
that is left behind.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.withings_device import WithingsDevice
from app.services.providers.withings.sdk_devices import (
    list_devices,
    mark_dissociated,
    record_installed_device,
    sync_devices_from_withings,
)
from tests.factories import UserConnectionFactory, UserFactory

_GETDEVICE = "app.services.providers.withings.sdk_devices.withings_request"


def _member(db: Session) -> UUID:
    """A member with a Withings connection, which is what devices hang off."""
    user = UserFactory()
    UserConnectionFactory(user=user, provider="withings", provider_user_id="withings-1")
    return user.id


def _entry(**overrides: object) -> dict:
    entry = {
        "deviceid": "device-1",
        "model": "Body+",
        "model_id": 6,
        "type": "Scale",
        "battery": "high",
        "last_session_date": 1_756_000_000,
    }
    entry.update(overrides)
    return entry


def _sync(db: Session, user_id: UUID, *entries: dict) -> list[WithingsDevice]:
    with patch(_GETDEVICE, return_value={"devices": list(entries)}):
        return sync_devices_from_withings(db, user_id=user_id, oauth=MagicMock())


class TestRecordInstalledDevice:
    def test_stores_what_the_notification_reported(self, db: Session) -> None:
        user_id = _member(db)

        device = record_installed_device(
            db,
            user_id=user_id,
            device_id="device-1",
            model_id=6,
            model="Body+",
        )

        assert device.device_id == "device-1"
        assert device.model_id == 6
        assert device.model == "Body+"
        assert device.dissociated_at is None

    def test_is_idempotent_on_the_same_device(self, db: Session) -> None:
        # The app may retry, and a member may re-run setup on a device they already own. The
        # unique (connection, device_id) index means the second write has to find the first.
        user_id = _member(db)

        first = record_installed_device(db, user_id=user_id, device_id="device-1", model="Body+")
        second = record_installed_device(db, user_id=user_id, device_id="device-1", model="Body Pro")

        assert second.id == first.id
        assert second.model == "Body Pro"
        assert db.query(WithingsDevice).count() == 1

    def test_records_a_device_that_reported_nothing_but_its_id(self, db: Session) -> None:
        # Every field but deviceid is optional. Refusing a sparse notification would lose the
        # device record along with the detail it legitimately does not have.
        user_id = _member(db)

        device = record_installed_device(db, user_id=user_id, device_id="device-1")

        assert device.device_id == "device-1"
        assert device.model is None
        assert device.battery is None


class TestSyncDevicesFromWithings:
    def test_stores_everything_getdevice_listed(self, db: Session) -> None:
        user_id = _member(db)

        devices = _sync(db, user_id, _entry())

        assert len(devices) == 1
        device = devices[0]
        assert device.model_id == 6
        assert device.device_type == "Scale"
        assert device.battery == "high"
        assert device.last_session_at == datetime.fromtimestamp(1_756_000_000, tz=timezone.utc)

    def test_accepts_the_older_modelid_spelling(self, db: Session) -> None:
        # Withings' responses have carried both spellings. Parsed as absent, model_id is null
        # and the setup WebView cannot be opened straight onto the right device.
        #
        # Written without a `model_id` key at all, rather than with a null one: AliasChoices
        # takes the FIRST alias PRESENT in the input, so `{"model_id": None, "modelid": 45}`
        # resolves to None and the test would pass for the wrong reason.
        user_id = _member(db)

        devices = _sync(db, user_id, {"deviceid": "device-1", "modelid": 45})

        assert devices[0].model_id == 45

    def test_never_erases_a_field_getdevice_did_not_carry(self, db: Session) -> None:
        # THE invariant. An omitted field means "this device did not report one", and reading
        # it as "no value" would wipe what an earlier response did carry.
        user_id = _member(db)
        _sync(db, user_id, _entry(battery="high"))

        devices = _sync(db, user_id, _entry(battery=None))

        assert devices[0].battery == "high"

    def test_a_later_value_wins_over_an_earlier_one(self, db: Session) -> None:
        # Preserving a value is not the same as freezing it: when Getdevice DOES carry one, it
        # is the current one, and a battery that fell from high to low must be readable.
        user_id = _member(db)
        _sync(db, user_id, _entry(battery="high"))

        devices = _sync(db, user_id, _entry(battery="low"))

        assert devices[0].battery == "low"

    def test_marks_devices_withings_no_longer_lists(self, db: Session) -> None:
        # How a dissociation performed inside Withings' settings WebView reaches us. Both
        # devices are listed once first, so both are things Getdevice has actually seen.
        user_id = _member(db)
        _sync(db, user_id, _entry(deviceid="device-1"), _entry(deviceid="device-2", battery="low"))

        _sync(db, user_id, _entry(deviceid="device-1"))

        gone = db.query(WithingsDevice).filter(WithingsDevice.device_id == "device-2").one()
        assert gone.dissociated_at is not None
        # Soft, so the row survives whole: a transient omission must not destroy its history.
        assert gone.battery == "low"

    def test_never_dissociates_a_device_getdevice_has_not_listed_yet(self, db: Session) -> None:
        # The freshly-paired case, and the reason last_getdevice_at exists. A member pairs a
        # scale, the app syncs before Withings' own list catches up — and this device must
        # NOT be swept, because its absence says nothing. record_installed_device's docstring
        # asserts exactly this lag; the sweep used to ignore it.
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="just-paired", model="Body+")

        _sync(db, user_id)

        device = db.query(WithingsDevice).filter(WithingsDevice.device_id == "just-paired").one()
        assert device.dissociated_at is None, "a device Getdevice has never listed cannot be judged by its absence"
        assert list_devices(db, user_id=user_id)[0].device_id == "just-paired"

    def test_a_sparse_getdevice_entry_still_counts_as_listed(self, db: Session) -> None:
        # The sweep's fact is "Getdevice has listed this device", and nothing else. An entry
        # that carries only a deviceid still says that, so a device known first from the
        # notification becomes sweepable once Getdevice mentions it at all.
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", model="Body+")
        _sync(db, user_id, {"deviceid": "device-1"})

        listed = db.query(WithingsDevice).filter(WithingsDevice.device_id == "device-1").one()
        assert listed.model == "Body+", "the sparse entry did not erase what the notification gave"
        assert listed.last_getdevice_at is not None, "but Getdevice has listed it, and that is the sweep's fact"

        _sync(db, user_id)

        db.refresh(listed)
        assert listed.dissociated_at is not None

    def test_an_empty_response_marks_previously_listed_devices_dissociated(self, db: Session) -> None:
        user_id = _member(db)
        _sync(db, user_id, _entry(deviceid="device-1"))

        assert _sync(db, user_id) == []

        device = db.query(WithingsDevice).one()
        assert device.dissociated_at is not None
        assert device.battery == "high"

    def test_seeing_a_device_again_undissociates_it(self, db: Session) -> None:
        user_id = _member(db)
        _sync(db, user_id, _entry(deviceid="device-1", battery="high"))
        _sync(db, user_id)

        devices = _sync(db, user_id, _entry(battery=None))

        assert devices[0].dissociated_at is None
        assert devices[0].battery == "high", "the row came back whole, not as a new one"
        assert db.query(WithingsDevice).count() == 1

    def test_one_members_devices_do_not_reach_another(self, db: Session) -> None:
        # The upsert key is (connection, device_id), not device_id — two members can own the
        # same model, and Withings device ids are not ours to assume unique across accounts.
        first = _member(db)
        second = _member(db)
        _sync(db, first, _entry(deviceid="device-1", battery="high"))

        _sync(db, second, _entry(deviceid="device-1", battery="low"))

        assert list_devices(db, user_id=first)[0].battery == "high"
        assert list_devices(db, user_id=second)[0].battery == "low"


class TestMarkDissociatedAndList:
    def test_marks_the_device_and_drops_it_from_the_default_list(self, db: Session) -> None:
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", model="Body+")

        assert mark_dissociated(db, user_id=user_id, device_id="device-1") is not None

        assert list_devices(db, user_id=user_id) == []
        assert len(list_devices(db, user_id=user_id, include_dissociated=True)) == 1

    def test_dissociating_an_unknown_device_is_not_an_error(self, db: Session) -> None:
        # The member may have dissociated one set up before we started recording devices, or
        # from another phone.
        user_id = _member(db)

        assert mark_dissociated(db, user_id=user_id, device_id="never-seen") is None
