"""Two sources for one advertise_key, and the rule that keeps them from destroying each other.

Background BLE sync does not start without a per-device ``advertise_key``, and Withings hands
it out two ways — the SDK's install-success notification, and ``User v2 - Getdevice``. Neither
is complete: the notification is the only source for a device Getdevice has not caught up with,
and Getdevice is the only source after an app reinstall, which loses every notification.

So the invariant these tests exist for is the one that is easy to break by writing the obvious
code: **a write never erases a key it cannot replace.** A Getdevice entry with no
``advertise_key`` must leave the stored one alone.

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
    SOURCE_GETDEVICE,
    SOURCE_NOTIFICATION,
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
        "advertise_key": "adv-from-getdevice",
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
            advertise_key="adv-1",
        )

        assert device.device_id == "device-1"
        assert device.advertise_key == "adv-1"
        assert device.advertise_key_source == SOURCE_NOTIFICATION
        assert device.dissociated_at is None

    def test_is_idempotent_on_the_same_device(self, db: Session) -> None:
        # The app may retry, and a member may re-run setup on a device they already own. The
        # unique (connection, device_id) index means the second write has to find the first.
        user_id = _member(db)

        first = record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-1")
        second = record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-2")

        assert second.id == first.id
        assert second.advertise_key == "adv-2"
        assert db.query(WithingsDevice).count() == 1

    def test_records_a_device_that_reported_no_key(self, db: Session) -> None:
        # A Wi-Fi device that never fell back to BLE. Refusing it would lose the device record
        # along with the key it legitimately does not have.
        user_id = _member(db)

        device = record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key=None)

        assert device.advertise_key is None
        assert device.advertise_key_source is None


class TestSyncDevicesFromWithings:
    def test_stores_everything_getdevice_listed(self, db: Session) -> None:
        user_id = _member(db)

        devices = _sync(db, user_id, _entry())

        assert len(devices) == 1
        device = devices[0]
        assert device.model_id == 6
        assert device.device_type == "Scale"
        assert device.advertise_key == "adv-from-getdevice"
        assert device.advertise_key_source == SOURCE_GETDEVICE
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

    def test_never_erases_a_key_getdevice_did_not_carry(self, db: Session) -> None:
        # THE invariant. The notification is often the only source of a just-installed
        # device's key, and nothing can re-derive it.
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-from-notification")

        devices = _sync(db, user_id, _entry(advertise_key=None))

        assert devices[0].advertise_key == "adv-from-notification"
        assert devices[0].advertise_key_source == SOURCE_NOTIFICATION

    def test_a_later_key_wins_over_an_earlier_one(self, db: Session) -> None:
        # Preserving a key is not the same as freezing it: when Getdevice DOES carry one, it
        # is the current one.
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-old")

        devices = _sync(db, user_id, _entry(advertise_key="adv-new"))

        assert devices[0].advertise_key == "adv-new"
        assert devices[0].advertise_key_source == SOURCE_GETDEVICE

    def test_marks_devices_withings_no_longer_lists(self, db: Session) -> None:
        # How a dissociation performed inside Withings' settings WebView reaches us. Both
        # devices are listed once first, so both are things Getdevice has actually seen.
        user_id = _member(db)
        _sync(db, user_id, _entry(deviceid="device-1"), _entry(deviceid="device-2", advertise_key="adv-2"))

        _sync(db, user_id, _entry(deviceid="device-1"))

        gone = db.query(WithingsDevice).filter(WithingsDevice.device_id == "device-2").one()
        assert gone.dissociated_at is not None
        # Soft, so the key survives: a transient omission must not destroy what only the
        # install notification ever carried.
        assert gone.advertise_key == "adv-2"

    def test_never_dissociates_a_device_getdevice_has_not_listed_yet(self, db: Session) -> None:
        # The freshly-paired case, and the reason last_getdevice_at exists. A member pairs a
        # scale, the app syncs before Withings' own list catches up — and this device must
        # NOT be swept, because its absence says nothing. record_installed_device's docstring
        # asserts exactly this lag; the sweep used to ignore it.
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="just-paired", advertise_key="adv-new")

        _sync(db, user_id)

        device = db.query(WithingsDevice).filter(WithingsDevice.device_id == "just-paired").one()
        assert device.dissociated_at is None, "a device Getdevice has never listed cannot be judged by its absence"
        assert list_devices(db, user_id=user_id)[0].device_id == "just-paired"

    def test_the_guard_is_not_advertise_key_source(self, db: Session) -> None:
        # The field that looks right is not: a Getdevice entry carrying NO advertise_key
        # leaves advertise_key_source saying "notification", so a sweep keyed on it would
        # still refuse to sweep a device Getdevice knows perfectly well about.
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-1")
        _sync(db, user_id, _entry(deviceid="device-1", advertise_key=None))

        listed = db.query(WithingsDevice).filter(WithingsDevice.device_id == "device-1").one()
        assert listed.advertise_key_source == SOURCE_NOTIFICATION, "the key still came from the notification"
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
        assert device.advertise_key == "adv-from-getdevice"

    def test_seeing_a_device_again_undissociates_it(self, db: Session) -> None:
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-1")
        _sync(db, user_id, _entry(deviceid="device-1", advertise_key=None))
        _sync(db, user_id)

        devices = _sync(db, user_id, _entry(advertise_key=None))

        assert devices[0].dissociated_at is None
        assert devices[0].advertise_key == "adv-1", "the row came back whole, not as a new one"
        assert db.query(WithingsDevice).count() == 1

    def test_one_members_devices_do_not_reach_another(self, db: Session) -> None:
        # The upsert key is (connection, device_id), not device_id — two members can own the
        # same model, and Withings device ids are not ours to assume unique across accounts.
        first = _member(db)
        second = _member(db)
        record_installed_device(db, user_id=first, device_id="device-1", advertise_key="adv-first")

        _sync(db, second, _entry(deviceid="device-1", advertise_key="adv-second"))

        assert list_devices(db, user_id=first)[0].advertise_key == "adv-first"
        assert list_devices(db, user_id=second)[0].advertise_key == "adv-second"


class TestMarkDissociatedAndList:
    def test_marks_the_device_and_drops_it_from_the_default_list(self, db: Session) -> None:
        user_id = _member(db)
        record_installed_device(db, user_id=user_id, device_id="device-1", advertise_key="adv-1")

        assert mark_dissociated(db, user_id=user_id, device_id="device-1") is not None

        assert list_devices(db, user_id=user_id) == []
        assert len(list_devices(db, user_id=user_id, include_dissociated=True)) == 1

    def test_dissociating_an_unknown_device_is_not_an_error(self, db: Session) -> None:
        # The member may have dissociated one set up before we started recording devices, or
        # from another phone.
        user_id = _member(db)

        assert mark_dissociated(db, user_id=user_id, device_id="never-seen") is None
